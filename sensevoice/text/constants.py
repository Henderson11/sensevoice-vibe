# sensevoice/text/constants.py
# 文本常量：语气词/噪声片段（识别结果清洗用）、技术词正则与停用词（项目术语表用）。

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
