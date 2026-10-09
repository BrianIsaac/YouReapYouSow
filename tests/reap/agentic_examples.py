"""Reap's own agentic example JSON, pasted verbatim from its docs.

Each example is the raw JSON text of a code block on the page named beside it, so the tests
parse exactly what Reap published. Placeholders such as ``<quote-id>`` are Reap's; where the
OpenAPI constrains a field (a UUID), the tests substitute a value and say so.
"""

import json
from typing import Any


def load(text: str) -> dict[str, Any]:
    """Parse one pasted example.

    Args:
        text: The example's JSON text.

    Returns:
        The decoded object.
    """
    return json.loads(text)


# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab "Reap card"
ENROLLMENT_REAP_CARD_REQUEST = """
{
  "source": "REAP_CARD",
  "cardId": "<card-id>"
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab "Reap card"
ENROLLMENT_REAP_CARD_RESPONSE = """
{
  "id": "<enrollment-id>",
  "status": "REQUIRES_ACTION",
  "source": "REAP_CARD",
  "owner": {
    "type": "REAP_USER",
    "id": "<user-id>"
  }
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab
# "BIN sponsor card"
ENROLLMENT_BIN_SPONSOR_REQUEST = """
{
  "source": "BIN_SPONSOR",
  "cardId": "<card-id>"
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab
# "BIN sponsor card"
ENROLLMENT_BIN_SPONSOR_RESPONSE = """
{
  "id": "<enrollment-id>",
  "status": "REQUIRES_ACTION",
  "source": "BIN_SPONSOR"
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab "External card"
ENROLLMENT_EXTERNAL_REQUEST = """
{
  "source": "EXTERNAL",
  "owner": {
    "type": "CLIENT_REFERENCE",
    "id": "<your-customer-id>",
    "email": "jsmith@example.com"
  },
  "presentation": {
    "type": "REDIRECT",
    "returnUrl": "https://example.com/cards/added"
  }
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Create the enrollment", tab "External card"
ENROLLMENT_EXTERNAL_RESPONSE = """
{
  "id": "<enrollment-id>",
  "status": "REQUIRES_ACTION",
  "source": "EXTERNAL",
  "owner": {
    "type": "CLIENT_REFERENCE",
    "id": "<your-customer-id>",
    "email": "jsmith@example.com"
  },
  "nextAction": {
    "type": "REDIRECT",
    "url": "<hosted-card-entry-url>",
    "expiresAt": "2026-01-01T00:00:00Z"
  }
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Confirm the enrollment is ACTIVE"
ENROLLMENT_RESPONSE = """
{
  "id": "<enrollment-id>",
  "status": "ACTIVE",
  "owner": {
    "type": "REAP_USER",
    "id": "<user-id>",
    "email": "jsmith@example.com"
  },
  "paymentMethod": {
    "type": "CARD",
    "network": "<network>",
    "last4": "4242",
    "expiryMonth": 11,
    "expiryYear": 2029
  },
  "nextAction": null,
  "createdAt": "2026-01-01T00:00:00Z",
  "updatedAt": "2026-01-01T00:00:00Z"
}
"""

# https://docs.reap.global/agentic-payments/setup.md, "Present the available enrollment cards"
ENROLLMENT_LIST_RESPONSE = """
{
  "items": [
    {
      "id": "<enrollment-id>",
      "status": "ACTIVE",
      "owner": {
        "type": "REAP_USER",
        "id": "<user-id>",
        "email": "jsmith@example.com"
      },
      "paymentMethod": {
        "type": "CARD",
        "network": "<network>",
        "last4": "4242",
        "expiryMonth": 11,
        "expiryYear": 2029
      },
      "nextAction": null,
      "createdAt": "2026-01-01T00:00:00Z",
      "updatedAt": "2026-01-01T00:00:00Z"
    }
  ],
  "nextCursor": null
}
"""

# https://docs.reap.global/agentic-payments/lifecycle.md, "Revoke an enrollment"
ENROLLMENT_REVOKED_RESPONSE = """
{
  "id": "<enrollment-id>",
  "status": "REVOKED",
  "owner": {
    "type": "CLIENT_REFERENCE",
    "id": "<your-customer-id>",
    "email": "jsmith@example.com"
  },
  "paymentMethod": {
    "type": "CARD",
    "network": "<network>",
    "last4": "4242",
    "expiryMonth": 11,
    "expiryYear": 2029
  },
  "nextAction": null,
  "createdAt": "2026-01-01T00:00:00Z",
  "updatedAt": "2026-01-01T00:00:00Z"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Search merchant catalogs"
SEARCH_REQUEST = """
{
  "query": "Sony WH 1000XM5 headphones",
  "context": {
    "country": "US",
    "currency": "USD"
  },
  "filters": {
    "price": {
      "min": "100",
      "max": "200"
    },
    "availability": "AVAILABLE_ONLY"
  },
  "pagination": {
    "limit": 20
  }
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Search merchant catalogs"
SEARCH_RESPONSE = """
{
  "id": "<search-id>",
  "products": [
    {
      "id": "<product-id>",
      "merchant": {
        "name": "<merchant-name>"
      },
      "name": "Sony WH 1000XM5 Wireless Headphone",
      "imageUrl": "https://example.com/image.jpg",
      "priceRange": {
        "min": { "amount": 129, "currency": "USD" },
        "max": { "amount": 149, "currency": "USD" }
      },
      "available": true,
      "previewVariant": {
        "id": "<variant-id>",
        "name": "<variant-name>",
        "price": { "amount": 129, "currency": "USD" },
        "available": true
      }
    }
  ],
  "pagination": {
    "nextCursor": "<cursor>",
    "hasNextPage": true,
    "returnedCount": 20
  },
  "warnings": []
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Read the product details"
DETAILS_REQUEST = """
{
  "productIds": ["<product-id>"]
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Read the product details"
DETAILS_RESPONSE = """
{
  "products": [
    {
      "id": "<product-id>",
      "name": "Sony WH 1000XM5 Wireless Headphones",
      "options": [
        {
          "name": "Color",
          "values": [
            { "optionId": "<option-id>", "label": "Black", "available": true },
            { "optionId": "<option-id>", "label": "Silver", "available": false }
          ]
        }
      ],
      "defaultVariant": {
        "id": "<variant-id>",
        "price": { "amount": 129, "currency": "USD" },
        "available": true,
        "requiresShipping": true
      }
    }
  ],
  "errors": []
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Resolve the variant"
VARIANT_REQUEST = """
{
  "productId": "<product-id>",
  "optionIds": ["<option-id>"]
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Resolve the variant"
VARIANT_RESPONSE = """
{
  "id": "<variant-id>",
  "name": "<variant-name>",
  "options": [
    { "name": "Color", "value": "Black" }
  ],
  "price": { "amount": 129, "currency": "USD" },
  "available": true,
  "requiresShipping": true
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Create the quote", tab
# "Reap discovery"
QUOTE_ITEMS_REQUEST = """
{
  "items": [{ "variantId": "var_123", "quantity": 1 }],
  "email": "avery.tan@reap.hk",
  "offerCode": "SAVE10"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Create the quote", tab
# "Checkout URL"
QUOTE_EXTERNAL_REQUEST = """
{
  "externalCheckout": {
    "merchantDomain": "merchant.example",
    "checkoutUrl": "https://merchant.example/cart/variant-1:1?attributes[partner_click_id]=example-123"
  },
  "email": "avery.tan@reap.hk",
  "shippingAddress": {
    "firstName": "Avery",
    "lastName": "Tan",
    "phone": "+85200000000",
    "addressLine1": "123 Example Street",
    "city": "Example City",
    "postalCode": "000000",
    "country": "HK"
  },
  "offerCode": "SAVE10"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Create the quote"
QUOTE_RESPONSE = """
{
  "id": "<quote-id>",
  "shippingOptions": [
    {
      "id": "<standard-option-id>",
      "name": "Standard",
      "selected": false,
      "price": { "amount": 5, "currency": "USD" }
    },
    {
      "id": "<express-option-id>",
      "name": "Express",
      "selected": true,
      "price": { "amount": 13, "currency": "USD" }
    }
  ],
  "amountBreakdown": {
    "itemsSubtotal": { "amount": 129, "currency": "USD" },
    "shipping": { "amount": 13, "currency": "USD" },
    "tax": {
      "amount": { "amount": 5, "currency": "USD" },
      "includedInPrices": false
    },
    "discounts": [],
    "additionalCharges": [],
    "finalAmount": { "amount": 142, "currency": "USD" }
  },
  "expiresAt": "2026-01-01T00:00:00Z"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Select a shipping option"
SHIPPING_OPTION_REQUEST = """
{
  "shippingOptionId": "<standard-option-id>"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Select a shipping option"
SHIPPING_OPTION_RESPONSE = """
{
  "id": "<quote-id>",
  "shippingOptions": [
    {
      "id": "<standard-option-id>",
      "name": "Standard",
      "selected": true,
      "price": { "amount": 5, "currency": "USD" }
    },
    {
      "id": "<express-option-id>",
      "name": "Express",
      "selected": false,
      "price": { "amount": 13, "currency": "USD" }
    }
  ],
  "amountBreakdown": {
    "itemsSubtotal": { "amount": 129, "currency": "USD" },
    "shipping": { "amount": 5, "currency": "USD" },
    "tax": {
      "amount": { "amount": 5, "currency": "USD" },
      "includedInPrices": false
    },
    "finalAmount": { "amount": 134, "currency": "USD" }
  },
  "expiresAt": "2026-01-01T00:00:00Z"
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Create the checkout"
CHECKOUT_REQUEST = """
{
  "quoteId": "<quote-id>",
  "enrollmentId": "<enrollment-id>",
  "presentation": {
    "type": "REDIRECT",
    "returnUrl": "https://example.com/orders/done"
  }
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Create the checkout"
CHECKOUT_CREATED_RESPONSE = """
{
  "id": "<checkout-id>",
  "status": "REQUIRES_ACTION",
  "quoteId": "<quote-id>",
  "enrollmentId": "<enrollment-id>",
  "amount": { "amount": 134, "currency": "USD" },
  "nextAction": {
    "type": "REDIRECT",
    "url": "<hosted-approval-url>",
    "expiresAt": "2026-01-01T00:00:00Z"
  }
}
"""

# https://docs.reap.global/agentic-payments/one-time-purchases.md, "Confirm the order"
CHECKOUT_RESPONSE = """
{
  "id": "<checkout-id>",
  "status": "COMPLETED",
  "quoteId": "<quote-id>",
  "enrollmentId": "<enrollment-id>",
  "orderId": "<merchant-order-id>",
  "finalAmount": { "amount": 134, "currency": "USD" },
  "nextAction": null,
  "createdAt": "2026-01-01T00:00:00Z",
  "updatedAt": "2026-01-01T00:00:00Z"
}
"""

# https://docs.reap.global/api-reference/errors.md, "Error Response Format"
ERROR_RESPONSE = """
{
  "error": {
    "code": "USER_NOT_FOUND",
    "message": "User not found",
    "detail": null
  }
}
"""

# https://docs.reap.global/api-reference/errors.md, "Validation errors"
VALIDATION_ERROR_RESPONSE = r"""
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "Validation failed",
    "detail": {
      "on": "body",
      "errors": [
        {
          "path": "type",
          "message": "Invalid option: expected one of \"VIRTUAL\"|\"PHYSICAL\"",
          "code": "invalid_value"
        }
      ]
    }
  }
}
"""

# https://docs.reap.global/api-reference/rate-limiting.md, "Exceeding the limit"
RATE_LIMIT_RESPONSE = """
{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Rate limit exceeded. See the Retry-After header for when to retry."
  }
}
"""
