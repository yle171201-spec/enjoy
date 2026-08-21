from .akshare_provider import AkShareProvider
from .tushare_provider import TushareProvider
from ..config import settings
def get_provider():return TushareProvider(settings.tushare_token) if settings.data_provider=="tushare" else AkShareProvider()
