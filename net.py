"""
net.py —— 网络层的统一初始化：让 Python 也使用操作系统的证书库

为什么需要这个：
    本机装了会做 HTTPS 中间人的工具（实测是 SteamTools，它把 github.com 的证书换成了
    自己的 "SteamTools Certificate"）。Windows 证书库里装了它的根证书，所以浏览器和
    git（用 schannel 后端时）都正常；但 Python 默认只认 certifi 里那份 Mozilla 证书列表，
    验不过就抛：
        SSLCertVerificationError: unable to get local issuer certificate

    注入 truststore 之后，Python 改用系统证书库，行为和浏览器一致。

安全性说明：
    这只是"信任你系统本来就信任的证书"，**没有关闭证书校验**，
    比自己写 verify=False 安全得多。如果你不希望那个工具解密自己的流量，
    正确做法是在那个工具里关掉 HTTPS 拦截（或把 GitHub 加进白名单）。
"""

from __future__ import annotations

import logging

log = logging.getLogger("byread.net")

ENABLED = False

try:
    import truststore  # type: ignore

    truststore.inject_into_ssl()
    ENABLED = True
except ImportError:
    # 没装 truststore 也能跑，只是访问"被中间人的站点"时会证书验证失败
    log.info("未安装 truststore，将只使用 certifi 证书列表")
except Exception as exc:  # noqa: BLE001
    log.warning("启用系统证书库失败：%s", exc)
