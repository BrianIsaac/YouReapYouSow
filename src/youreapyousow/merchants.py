"""How each provider appears as a card merchant.

A simulated authorisation carries the merchant name and category the caller chooses,
so the name and MCC here are what Reap records and what the external authorisation
check expects to see. MCC 7372 (computer programming, data processing and integrated
systems design) is a choice for cloud compute, not a researched fact about how any of
these providers is actually categorised.
"""

from dataclasses import dataclass

COMPUTE_MCC = "7372"
COMPUTE_MCC_CATEGORY = "Computer Programming, Data Processing"


@dataclass(frozen=True)
class Merchant:
    """A provider's merchant identity on a card transaction.

    Attributes:
        name: Merchant name.
        mcc_code: Merchant category code.
        mcc_category: Category description.
        country: ISO country code.
    """

    name: str
    mcc_code: str = COMPUTE_MCC
    mcc_category: str = COMPUTE_MCC_CATEGORY
    country: str = "US"


MERCHANTS: dict[str, Merchant] = {
    "vast": Merchant("Vast.ai"),
    "runpod": Merchant("RunPod"),
    "shadeform": Merchant("Shadeform"),
}


def merchant_for(provider: str) -> Merchant:
    """Return the merchant identity used when paying a provider.

    Args:
        provider: Provider name as it appears on offers.

    Returns:
        The known merchant, or one named after the provider with the compute MCC.
    """
    return MERCHANTS.get(provider, Merchant(provider))
