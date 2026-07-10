"""Company evidence providers used by the offline micro analyst."""

from trader.infrastructure.market_sources.company.esef import EsefCompanyEvidenceProvider
from trader.infrastructure.market_sources.company.finmind import FinMindCompanyEvidenceProvider
from trader.infrastructure.market_sources.company.local_news import LocalCompanyNewsEvidenceProvider
from trader.infrastructure.market_sources.company.registry import CompositeCompanyEvidenceProvider
from trader.infrastructure.market_sources.company.sec_edgar import SecEdgarCompanyEvidenceProvider
from trader.infrastructure.market_sources.company.yfinance_company import YFinanceCompanyEvidenceProvider

__all__ = [
    "CompositeCompanyEvidenceProvider",
    "EsefCompanyEvidenceProvider",
    "FinMindCompanyEvidenceProvider",
    "LocalCompanyNewsEvidenceProvider",
    "SecEdgarCompanyEvidenceProvider",
    "YFinanceCompanyEvidenceProvider",
]
