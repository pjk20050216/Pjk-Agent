"""意图识别 —— 正则快速过滤规则（零延迟，纯 Python 无网络调用）。

五类规则：
- instruction_override      指令覆盖（ignore previous instructions / 忽略之前的指令）
- roleplay_jailbreak        角色扮演 / 越狱（act as / 扮演 / DAN mode）
- system_prompt_extraction  系统提示诱导（reveal your prompt / 你的提示词是什么）
- marker_injection          标记注入（<|endoftext|> / [INST] / 零宽字符 / 控制字符）
- context_stuffing          上下文填塞（超长重复填充）
"""
import re

# 指令覆盖：诱导模型忽略既定指令/规则
INSTRUCTION_OVERRIDE = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier|your|the)\s+(instructions?|prompts?|rules?|directives?|guidelines?)",
    r"disregard\s+(all\s+|any\s+)?(previous|prior|above|earlier)",
    r"forget\s+(everything|all\s+(previous\s+|prior\s+)?(instructions?|rules?|prompts?))",
    r"override\s+(the\s+)?(system|previous|above|developer)",
    r"忽略(所有|任何|之前|上面|上述|以下|以上)?(的)?(指令|提示|提示词|规则|要求|约束|设定)",
    r"无视(上面|之前|所有|上述|以下)?(的)?(指令|规则|要求|约束)",
    r"不要(再)?(遵守|听从|执行|理会)(之前|上面|上述)?(的)?(指令|规则|要求)",
    r"从现在开始(你|请)?(要|必须|只|只能)",
    r"你(现在|从现在起|接下来)(是|扮演|要|必须)",
    r"推翻(之前|上面)?(的)?(指令|规则)",
]

# 角色扮演 / 越狱：诱导模型脱离安全边界
ROLEPLAY_JAILBREAK = [
    r"\bact\s+as\b",
    r"\bpretend\s+(to\s+be|you\s+are)\b",
    r"\byou\s+are\s+(now\s+)?(a\s+|an\s+)?(dan|jailbroken|uncensored|unrestricted)\b",
    r"\b(dan|jailbreak|developer|god)\s+mode\b",
    r"without\s+(any\s+)?(restrictions?|limits?|rules?)",
    r"扮演(一个|一名)?|角色扮演",
    r"假装(你是|成为)",
    r"越狱|无限制(模式)?|解除(所有)?限制",
    r"无所不能|没有(任何)?限制",
]

# 系统提示诱导：诱导模型泄露内部提示词/规则
SYSTEM_PROMPT_EXTRACTION = [
    r"(reveal|show|display|print|output|expose|leak|tell\s+me|give\s+me)\s+(your|the)\s+(system\s+|developer\s+)?(prompts?|instructions?|rules?|directives?|messages?)",
    r"what\s+(is|are)\s+(your|the)\s+(system\s+|developer\s+)?(prompts?|instructions?|rules?)",
    r"(system|developer)\s+(prompt|message|instructions?)",
    r"(显示|展示|打印|输出|泄露|泄漏|告诉|给我|说出)(你|你的)?(的)?(系统|内部)?(提示词|提示语|指令|规则|设定|提示)",
    r"(你|你的)(的)?(系统|内部)?(提示词|提示语|指令|设定)(是)?什么",
    r"(系统|内部)(提示词|提示语|指令)",
]

# 标记注入：特殊 token / 模板变量 / 不可见字符
MARKER_INJECTION = [
    r"<\|endoftext\|>",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
    r"<\|system\|>",
    r"<\|user\|>",
    r"<\|assistant\|>",
    r"<\|end\|>",
    r"\[INST\]",
    r"\[/INST\]",
    r"</s>",
    r"<s>",
    r"<<SYS>>",
    r"\{\{\s*[a-zA-Z_.\[\]]+\s*\}\}",
    r"[\u200b-\u200f\ufeff]",  # 零宽字符
    r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]",  # 控制字符
]

# 上下文填塞：单字符超长重复（多片段重复由 intent_guard 的长度启发式兜底）
CONTEXT_STUFFING = [
    r"(.)\1{80,}",
]

RULE_GROUPS: dict[str, list[str]] = {
    "instruction_override": INSTRUCTION_OVERRIDE,
    "roleplay_jailbreak": ROLEPLAY_JAILBREAK,
    "system_prompt_extraction": SYSTEM_PROMPT_EXTRACTION,
    "marker_injection": MARKER_INJECTION,
    "context_stuffing": CONTEXT_STUFFING,
}

# 预编译所有规则，匹配时忽略大小写
_COMPILED: dict[str, list[re.Pattern]] = {
    group: [re.compile(p, re.IGNORECASE) for p in patterns]
    for group, patterns in RULE_GROUPS.items()
}


def scan(content: str) -> list[tuple[str, str]]:
    """扫描文本，返回命中的 (类别, 匹配片段) 列表（每类最多一条）。"""
    hits: list[tuple[str, str]] = []
    for group, patterns in _COMPILED.items():
        for pat in patterns:
            m = pat.search(content)
            if m:
                hits.append((group, m.group(0)[:80]))
                break
    return hits
