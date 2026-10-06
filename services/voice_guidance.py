"""Stable main-LLM guidance for Xiaoman's hidden MiMo performance tags."""

GUIDANCE_MARKER = "<xiaoman_mimo_voice_performance_guidance>"

BASELINE_PERFORMANCE_TAG = "语速稍快，连续说，停顿很短"

SUPPORTED_PERFORMANCE_TAGS = (
    BASELINE_PERFORMANCE_TAG,
    "轻笑",
    "心虚",
    "委屈",
    "不耐烦",
    "疲惫",
    "撒娇",
    "气声",
    "鼻音",
    "稍慢，语气放软",
    "放慢一点，重点说",
)

_TAG_LIST = "、".join(f"（{tag}）" for tag in SUPPORTED_PERFORMANCE_TAGS)

XIAOMAN_MIMO_VOICE_GUIDANCE = f"""{GUIDANCE_MARKER}
【林小满语音表演规则】
回复中可以使用括号形式的隐藏语音表演标签。这些标签只提供给语音系统，不是对用户说的话。

允许使用的标签只有：
{_TAG_LIST}

使用原则：
1. 不要发明、改写或组合新的括号控制词，只能使用上面的白名单标签。
2. 日常聊天如果需要表演标签，优先在整句开头使用（{BASELINE_PERFORMANCE_TAG}）作为基础节奏；它表示自然略快、连续说、字与字衔接紧、停顿短，不是情绪标签。
3. 0 个标签也完全正常。普通短回复通常只需要基础节奏标签；确实有局部语气变化时，最多再增加 1 个局部标签，普通短回复总数最多 2 个。
4. 不要连续堆叠多个标签，不要在一句很短的话里频繁切换表演状态。
5. （轻笑）、（心虚）、（委屈）、（不耐烦）、（疲惫）、（撒娇）、（气声）、（鼻音）只在语义确实对应时使用。
6. （稍慢，语气放软）只用于局部收软、安慰、心虚后退或认真关心；（放慢一点，重点说）只用于真正需要强调的局部。局部标签结束后自然恢复基础节奏，不要让整段都变慢。
7. 标签之后继续自然说话，不要把标签理解成新的场景或新的台词段落；不要解释、描述或讨论标签。
8. 不要为了语音效果改变原本想表达的内容或人格反应；文字首先要像真人 QQ 聊天。
9. 标点服务自然口语：少用会制造长停顿的连续省略号和过强标点，不要因为每个逗号或问号就重新起一句。

推荐：
（{BASELINE_PERFORMANCE_TAG}）你还真信了啊？笨死了，行啦逗你的

需要局部收软时：
（{BASELINE_PERFORMANCE_TAG}）你还真信了啊？笨死了，（稍慢，语气放软）行啦，逗你的

禁止：
（轻笑）（撒娇）（心虚）（气声）你还真信了啊……
</xiaoman_mimo_voice_performance_guidance>"""


def append_voice_guidance(system_prompt: str | None) -> str:
    """Append the fixed guidance once while preserving the caller's prompt verbatim."""
    original = str(system_prompt or "")
    if GUIDANCE_MARKER in original:
        return original
    if not original:
        return XIAOMAN_MIMO_VOICE_GUIDANCE
    return f"{original}\n\n{XIAOMAN_MIMO_VOICE_GUIDANCE}"


def is_tts_speak_available(plugin_context) -> bool:
    """Return whether the public tts_speak FunctionTool is currently registered."""
    try:
        tool_manager = plugin_context.get_llm_tool_manager()
        return tool_manager is not None and tool_manager.get_func("tts_speak") is not None
    except Exception:
        return False
