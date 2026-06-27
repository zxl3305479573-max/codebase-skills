"""
Query Rewriter — 大白话→技术查询

将用户的非正式描述改写为精确的技术查询。
核心约束：只做等价替换，禁止脑补，不确定时保持原样。

使用 Claude API (Haiku) 进行改写，配合约束式 system prompt。
"""

import json
import sys
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    """查询意图分类"""
    BUG = "bug"           # 修复 bug
    FEATURE = "feature"   # 添加功能
    REFACTOR = "refactor" # 重构
    PERF = "perf"         # 性能优化
    LOCATE = "locate"     # 定位代码
    UNSURE = "unsure"     # 无法确定

    @classmethod
    def from_str(cls, value: str) -> "Intent":
        """安全地从字符串构造 Intent，无效值返回 UNSURE"""
        try:
            return cls(value)
        except ValueError:
            return cls.UNSURE


@dataclass
class RewriteResult:
    """改写结果"""
    original: str                          # 原始查询
    rewritten: str                         # 改写后的查询
    intent: Intent                         # 查询意图
    entities: list[str]                    # 对齐到的代码实体
    uncertainties: list[str]               # 因不确定而保持原样的部分
    needs_confirmation: bool = False       # 是否需要用户确认
    semantic_distance: float = 0.0         # 语义距离 (0~1)


class QueryRewriter:
    """
    约束式查询改写器

    使用 Claude API (Haiku) 将用户大白话改写为精确技术查询。
    通过 system prompt 约束：只做等价替换，禁止脑补。

    使用方法:
        rewriter = QueryRewriter()
        result = rewriter.rewrite("支付回调偶发空指针")
        if result.needs_confirmation:
            print(f"我理解你要: {result.rewritten}")
            # 等待用户确认
    """

    # 改写 system prompt
    SYSTEM_PROMPT = """你是代码查询改写器。将用户的非正式描述改写为精确的技术查询。

强制规则：
1. 只做等价替换 — 口语词 → 代码实体名，模糊描述 → 工程术语
2. 禁止推断 — 用户没说"高并发"，不能加"高并发"
3. 禁止扩大范围 — 用户说"改登录"，不能写成"重构认证系统"
4. 不确定时保持原样 — 宁可保留模糊，也不脑补确定

输出 JSON：
{
  "rewritten": "<改写后的精确查询>",
  "intent": "bug|feature|refactor|perf|locate|unsure",
  "entities": ["<对齐到的函数名/类名/文件名>"],
  "uncertainties": ["<因不确定而保持原样的部分>"]
}"""

    CONFIRMATION_PROMPT = """判断以下两个查询的语义距离（0~1之间的小数）：
原始查询：{original}
改写查询：{rewritten}

评分标准：
- 0.0~0.3: 纯实体替换（如"支付"→"handle_payment"），语义完全等价
- 0.3~0.7: 补充了结构信息或缩小了范围，但核心意图不变
- 0.7~1.0: 可能改变了用户意图或大幅扩展了范围

只输出一个0~1之间的数字。"""

    def __init__(self, model: str = "claude-haiku-4-5-20251001",
                 small_threshold: float = 0.3,
                 large_threshold: float = 0.7):
        """
        Args:
            model: Claude 模型 ID（Haiku 推荐，改写不需要复杂推理）
            small_threshold: 语义距离 ≤ 此值则跳过确认
            large_threshold: 语义距离 ≥ 此值则强制确认
        """
        self.model = model
        self.small_threshold = small_threshold
        self.large_threshold = large_threshold
        self._client = None
        self._client_error: str | None = None

    @property
    def client(self):
        """惰性初始化 Anthropic 客户端"""
        if self._client is None and self._client_error is None:
            try:
                from anthropic import Anthropic
                self._client = Anthropic()
            except ImportError as e:
                self._client_error = str(e)
        if self._client is None:
            raise RuntimeError(
                f"Anthropic SDK not available: {self._client_error}. "
                "Install: pip install anthropic>=0.30, or use --skip-rewrite"
            )
        return self._client

    @property
    def available(self) -> bool:
        """客户端是否可用"""
        if self._client is not None:
            return True
        if self._client_error is not None:
            return False
        try:
            from anthropic import Anthropic
            return True
        except ImportError:
            return False

    def rewrite(self, query: str, force_confirm: bool = False) -> RewriteResult:
        """
        改写查询

        Args:
            query: 用户原始查询（大白话）
            force_confirm: 是否强制要求确认

        Returns:
            RewriteResult 包含改写结果和确认建议
        """
        if not query.strip():
            return RewriteResult(
                original=query,
                rewritten=query,
                intent=Intent.UNSURE,
                entities=[],
                uncertainties=["查询为空"],
                needs_confirmation=False,
            )

        # Step 1: 约束式改写
        rewrite_data = self._call_rewrite_api(query)

        rewritten = rewrite_data.get("rewritten", query)
        intent_str = rewrite_data.get("intent", "unsure")
        entities = rewrite_data.get("entities", [])
        uncertainties = rewrite_data.get("uncertainties", [])

        # Step 2: 语义距离判断
        if rewritten == query:
            # 改写没有变化 → 不需要确认
            return RewriteResult(
                original=query,
                rewritten=rewritten,
                intent=Intent.from_str(intent_str),
                entities=entities,
                uncertainties=uncertainties,
                needs_confirmation=False,
                semantic_distance=0.0,
            )

        semantic_distance = self._evaluate_distance(query, rewritten)

        # 判断是否需要确认
        needs_confirmation = (
            force_confirm or
            semantic_distance >= self.large_threshold or
            len(uncertainties) > 0
        )

        return RewriteResult(
            original=query,
            rewritten=rewritten,
            intent=Intent.from_str(intent_str),
            entities=entities,
            uncertainties=uncertainties,
            needs_confirmation=needs_confirmation,
            semantic_distance=semantic_distance,
        )

    def _call_rewrite_api(self, query: str) -> dict:
        """调用 Claude API 进行改写"""
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                system=self.SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": query}
                ],
                temperature=0.1,  # 低温度保证确定性输出
            )
            text = response.content[0].text
            return self._parse_json_response(text)
        except Exception as e:
            print(f"[rewrite] API call failed: {e}", file=sys.stderr)
            return {
                "rewritten": query,
                "intent": "unsure",
                "entities": [],
                "uncertainties": [str(e)],
            }

    def _evaluate_distance(self, original: str, rewritten: str) -> float:
        """评估原始查询和改写查询之间的语义距离"""
        prompt = self.CONFIRMATION_PROMPT.format(
            original=original,
            rewritten=rewritten,
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=20,
                temperature=0,
                messages=[
                    {"role": "user", "content": prompt}
                ],
            )
            text = response.content[0].text.strip()
            # 提取数字
            import re
            match = re.search(r'(\d+\.?\d*)', text)
            if match:
                return float(match.group(1))
            return 0.5  # 默认中等距离
        except Exception:
            return 0.5  # API 失败时默认中等距离

    @staticmethod
    def _parse_json_response(text: str) -> dict:
        """从 API 响应中解析 JSON"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试提取 ```json ... ``` 代码块
        import re
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试提取第一个 { ... } 对象
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        # 回退：原样返回
        return {"rewritten": text.strip(), "intent": "unsure",
                "entities": [], "uncertainties": ["failed to parse JSON response"]}

    def format_confirmation(self, result: RewriteResult) -> str:
        """格式化确认提示"""
        intent_labels = {
            Intent.BUG: "bug 修复",
            Intent.FEATURE: "功能开发",
            Intent.REFACTOR: "重构",
            Intent.PERF: "性能优化",
            Intent.LOCATE: "代码定位",
            Intent.UNSURE: "不确定意图",
        }

        lines = ["我理解你要："]
        lines.append(f"  【{intent_labels.get(result.intent, result.intent.value)}】{result.rewritten}")

        if result.entities:
            lines.append(f"  涉及实体：{', '.join(result.entities)}")

        if result.uncertainties:
            lines.append(f"  未确定的部分：{', '.join(result.uncertainties)}")

        lines.append("")
        lines.append("→ 按 Enter 确认，或修改描述后重新检索")

        return "\n".join(lines)


# ──── CLI ────

def main():
    """CLI 入口：测试 Query Rewriter"""
    import argparse

    parser = argparse.ArgumentParser(description="Query Rewriter")
    parser.add_argument("query", nargs="+", help="查询文本（大白话）")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001",
                       help="Claude 模型 ID")
    parser.add_argument("--force-confirm", action="store_true",
                       help="强制要求确认")
    parser.add_argument("--json", action="store_true",
                       help="以 JSON 格式输出（默认已输出 JSON）")
    args = parser.parse_args()

    query = " ".join(args.query)

    rewriter = QueryRewriter(model=args.model)
    result = rewriter.rewrite(query, force_confirm=args.force_confirm)

    output = {
        "original": result.original,
        "rewritten": result.rewritten,
        "intent": result.intent.value,
        "entities": result.entities,
        "uncertainties": result.uncertainties,
        "needs_confirmation": result.needs_confirmation,
        "semantic_distance": result.semantic_distance,
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))

    if result.needs_confirmation:
        print("\n" + "=" * 60, file=sys.stderr)
        print(rewriter.format_confirmation(result), file=sys.stderr)


if __name__ == "__main__":
    main()
