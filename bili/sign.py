"""wbi 签名（B 站 Web 接口签名方案）

流程：
1. /x/web-interface/nav 返回 img_url / sub_url，各取文件名主干，拼成 64 位原始串
2. 按固定重排表取前 32 位 -> mixin_key
3. 参数按 key 升序，剔除 !'()* ，拼 query，末尾拼 mixin_key，md5 -> w_rid
4. 追加 wts 时间戳
"""

import hashlib
import re
import time
from urllib.parse import quote

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_STRIP = re.compile(r"[!'()*]")


def extract_key(img_url: str, sub_url: str) -> str:
    """两个 wbi 图片 url -> 64 位原始串"""
    def stem(u: str) -> str:
        name = u.rsplit("/", 1)[-1]
        return name.split(".")[0]
    return stem(img_url) + stem(sub_url)


def mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB[:32])


def sign_params(params: dict, mixin: str) -> dict:
    """就地返回带 wts / w_rid 的新参数字典"""
    p = {k: str(v) for k, v in params.items()}
    p["wts"] = str(int(time.time()))
    query = "&".join(
        f"{quote(k, safe='')}={quote(_STRIP.sub('', v), safe='')}"
        for k, v in sorted(p.items())
    )
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p
