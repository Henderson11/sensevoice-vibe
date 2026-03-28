# sensevoice/text/constants.py
# Text processing constants: filler words, punctuation sets, regex patterns,
# and token classification sets used by sanitization and confidence scoring.

import re

FILLER_WORDS_EN = {
    "yeah",
    "ok",
    "okay",
    "uh",
    "um",
    "hmm",
    "ah",
    "eh",
    "huh",
    "mm",
    "mhm",
    "hmmhmm",
    "i",
}
SHORT_NOISE_ZH = {
    "嗯",
    "啊",
    "额",
    "诶",
    "欸",
    "哦",
    "喔",
    "哎",
    "唉",
    "呀",
    "哈",
    "呃",
    "我",
}
PUNCT_EDGE = " \t\r\n.,，。!?！？、~～:;；'\"\u201c\u201d\u2018\u2019`()[]{}<>+-_/\\|"
# SenseVoice rich postprocess may emit emotion/event symbols (emoji). Strip them
# for coding dictation to avoid non-text artifacts in prompts.
EMOJI_ARTIFACTS = "😊😔😡😰🤢😮🎼👏😀😭🤧❓"
WAKE_WORD_STRIP_EDGE = " \t\r\n,，。.!?！？、:：;；-—_"
TECH_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:=+-]{2,}")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
COMMON_TECH_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "from",
    "this",
    "true",
    "false",
    "none",
    "null",
    "return",
    "class",
    "def",
    "import",
    "const",
    "let",
    "var",
    "function",
    "async",
    "await",
    "public",
    "private",
    "static",
    "final",
    "void",
    "int",
    "float",
    "double",
    "string",
}
CONF_PUNCT_TOKENS = set(".,，。!?！？、:;；()[]{}<>\"'`\u201c\u201d\u2018\u2019")
CONF_LOW_VALUE_ZH = set("的地得了着过吗么呢啊呀吧嘛将把被与和及并在是有就也都又还才")
