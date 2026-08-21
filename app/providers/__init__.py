from .akshare_provider import AkShareProvider
from .tushare_provider import TushareProvider
from .public_provider import PublicDataProvider
from ..config import settings


def get_provider():
    p = settings.data_provider
    if p == "tushare":
        return TushareProvider(settings.tushare_token)
    if p in {"public", "baostock", "auto"}:
        return PublicDataProvider()
    return AkShareProvider()
