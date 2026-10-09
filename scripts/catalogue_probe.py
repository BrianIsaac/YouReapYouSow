"""Read the Reap catalogue for the three ``purchase:`` files, print it and snapshot it.

    uv run python -m scripts.catalogue_probe                    # the three shipped files
    uv run python -m scripts.catalogue_probe --config configs/purchase-part.yaml
    uv run python -m scripts.catalogue_probe --query "120mm case fan"   # free searches only
    uv run python -m scripts.catalogue_probe --quote            # and each candidate's quote
    uv run python -m scripts.catalogue_probe --estimate-usd 0.80    # judge credits by it

The catalogue read once the sandbox key is in ``.env``. On the backend ``.env`` names
(the in-process mock by default; the sandbox with ``REAP_BACKEND=sandbox`` and the key),
each file's search is run as the control plane runs it
(``POST /agentic/products/search`` with the file's query, context, price band and
availability), the first ten products are expanded (``POST /agentic/products/details``),
and each option combination the run would resolve is resolved
(``POST /agentic/products/variant``) and judged by the gate's rule 10
(``procure.need.judge_variant``) against the file's match, with whether its merchant is
in the file's merchant scope. Every product is shown, in scope or not, so the scope can
be edited to the real catalogue. Those three calls only read.

``--quote`` also asks ``POST /agentic/quotes`` for each candidate that passes rule 10
(and for the checkout-URL file's cart) and prints the landed breakdown, ``finalAmount``
and ``expiresAt``, read against the file's grant: allowed, escalated or refused on the
per-purchase cap. A quote moves no money and is never checked out here: there is no
checkout call in this script. Each quote's key is ``probe:<run>:<variant>``.

Shape (b)'s parts are named in the machine's bill of materials (``configs/machine.yaml``,
or ``AGENT_MACHINE_CONFIG``; ``--machine`` names another): each part's query, price band
and accepted option values are probed under the part-shaped purchase file, as the live
session buys them (the file in force when it is shape ``part``, else
``purchase-part.yaml``), when the default files are probed or ``--machine`` is given.
The file is read by its documented shape, not through the session's code.

Everything Reap answered is written, as it answered, to
``var/catalogue-probe/probe-<UTC time>-<backend>.json`` (``--no-snapshot`` to skip). The
key is never printed or written.

Choices made here: option combinations are resolved as the control plane's
``gather_quotes`` resolves them (each available value, filtered by an attribute match,
at most eight per product: ``AgenticSettings.max_variants_per_product``), so what the
probe shows as a candidate is what the run would quote; a product whose options the
match leaves no value for is shown with its default variant judged, so the reason reads.
A value match (shape (a)) without ``--estimate-usd`` is valued, not judged.
"""

import argparse
import asyncio
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from scripts.swap_check import reap_for
from youreapyousow.api.status import purchase_config_path, shown_path
from youreapyousow.clock import utc_now
from youreapyousow.config import ConfigError, Settings
from youreapyousow.control import AgenticSettings
from youreapyousow.domain import BUDGET_CURRENCY, CatalogueVariant
from youreapyousow.procure.need import (
    AttributeMatch,
    CatalogueSearch,
    Need,
    NeedSpec,
    PriceBand,
    Route,
    SearchPlace,
    Shape,
    ValueMatch,
    judge_variant,
)
from youreapyousow.procure.selection import in_scope
from youreapyousow.purchase import (
    PURCHASE_CONFIG_DIR,
    PurchaseConfig,
    PurchaseGrant,
    ScenarioError,
    load_purchase,
)
from youreapyousow.reap.client import (
    ReapClient,
    ReapError,
    ReapTransportError,
)
from youreapyousow.reap.models import (
    AgenticErrorCode,
    CreateExternalCheckoutQuoteRequest,
    CreateItemsQuoteRequest,
    ExternalCheckout,
    Money,
    ProductDetail,
    ProductDetailsRequest,
    ProductDetailsResponse,
    ProductSearchRequest,
    ProductSearchResponse,
    ProductSummary,
    Quote,
    QuoteItem,
    ResolveVariantRequest,
    Variant,
)

SHIPPED = tuple(
    PURCHASE_CONFIG_DIR / name
    for name in ("purchase-compute.yaml", "purchase-part.yaml", "purchase-checkout-url.yaml")
)
MACHINE_CONFIG = PURCHASE_CONFIG_DIR / "machine.yaml"
SNAPSHOT_DIR = Path("var/catalogue-probe")
DETAILS_LIMIT = 10
MAX_VARIANTS_PER_PRODUCT = AgenticSettings().max_variants_per_product
_NO_ESTIMATE = "not judged: pass --estimate-usd for the live estimate"


class ProbeError(ValueError):
    """Raised when a file the probe reads does not load."""


class _BomPart(BaseModel):
    """One part of the machine's bill of materials, as ``configs/machine.yaml`` writes it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    part: str = Field(min_length=1)
    query: str = Field(min_length=1)
    price: PriceBand | None = None
    accept: dict[str, tuple[str, ...]] = Field(min_length=1)


class _MachineFile(BaseModel):
    """The machine file's bill of materials; its sensors are the session's, not read here."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    bom: dict[str, _BomPart] = Field(min_length=1)


class _MachineSetting(BaseSettings):
    """``AGENT_MACHINE_CONFIG``, from the environment or ``.env``, as the session reads it."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    machine_config: Path = MACHINE_CONFIG


@dataclass(frozen=True)
class Candidate:
    """A variant the run would resolve, judged by rule 10.

    Attributes:
        product_id: The product.
        product: The product's name.
        merchant: The merchant.
        in_scope: Whether the merchant is in the file's merchant scope; None for a free
            search, which has no scope.
        variant: The variant as the control plane records it.
        raw: The variant as Reap sent it.
        fills: Rule 10's verdict; None when not judged.
        judgement: Rule 10's detail, or why it was not judged.
        quote: The landed quote, with ``--quote``.
        quote_error: Why no quote landed, with ``--quote``.
    """

    product_id: str
    product: str
    merchant: str
    in_scope: bool | None
    variant: CatalogueVariant
    raw: Variant
    fills: bool | None
    judgement: str
    quote: Quote | None = None
    quote_error: str | None = None


@dataclass(frozen=True)
class ProductRead:
    """A search result, expanded.

    Attributes:
        summary: The search result.
        detail: Its options and default variant, if details answered for it.
        candidates: The variants resolved for it.
        note: Why it has no candidate, if it has none.
    """

    summary: ProductSummary
    detail: ProductDetail | None
    candidates: tuple[Candidate, ...]
    note: str | None = None


@dataclass(frozen=True)
class Probe:
    """One search, everything it led to, and Reap's raw answers.

    Attributes:
        label: The file or free search it came from.
        request: The search sent; None on the checkout-URL route.
        search: The search response.
        details: The details response.
        products: Each result, expanded.
        error: Why the search failed, if it did.
        note: What the probe did not do, and why.
        cart_quote: The checkout-URL route's cart quote, with ``--quote``.
        cart_error: Why the cart did not quote.
    """

    label: str
    request: ProductSearchRequest | None
    search: ProductSearchResponse | None = None
    details: ProductDetailsResponse | None = None
    products: tuple[ProductRead, ...] = field(default_factory=tuple[ProductRead, ...])
    error: str | None = None
    note: str | None = None
    cart_quote: Quote | None = None
    cart_error: str | None = None


def _refused(error: ReapError | ReapTransportError) -> str:
    if isinstance(error, ReapError):
        reason = (error.detail or {}).get("reason")
        return f"{error.code} ({reason})" if reason else error.code
    return f"Reap not reached: {error}"


def _option_sets(
    product: ProductDetail, match: AttributeMatch | ValueMatch | None
) -> list[list[str]]:
    """List the option combinations the control plane would resolve for a product.

    Args:
        product: The product's details.
        match: The file's match; an attribute match leaves out values it does not accept.

    Returns:
        Up to ``MAX_VARIANTS_PER_PRODUCT`` combinations of option ids; one empty
        combination for a product without options; none when a group has no value left.
    """
    groups: list[list[str]] = []
    for option in product.options:
        values = [
            v.option_id
            for v in option.values
            if v.available is not False
            and (not isinstance(match, AttributeMatch) or match.accepts(option.name, v.label))
        ]
        if not values:
            return []
        groups.append(values)
    combinations = itertools.product(*groups)
    return [list(c) for c in itertools.islice(combinations, MAX_VARIANTS_PER_PRODUCT)]


def _need(spec: NeedSpec, estimate_usd: Decimal | None) -> tuple[Need, bool]:
    """Raise the need rule 10 judges against, as the run would.

    Args:
        spec: The file's need.
        estimate_usd: The live estimate, for a value match.

    Returns:
        The need, and whether its value match had to go unjudged for want of an estimate.
    """
    match = spec.match
    unjudged = isinstance(match, ValueMatch) and match.needs_estimate and estimate_usd is None
    if unjudged:
        assert isinstance(match, ValueMatch)
        spec = spec.model_copy(
            update={
                "match": match.model_copy(update={"covers_estimate": False, "max_multiple": None})
            }
        )
    need = Need(
        id="probe",
        objective_id="probe",
        spec=spec,
        reason="catalogue probe",
        estimate_usd=None if unjudged else estimate_usd,
        raised_at=utc_now(),
    )
    return need, unjudged


def _items_quote(spec: NeedSpec, variant: CatalogueVariant) -> CreateItemsQuoteRequest | str:
    ships = variant.requires_shipping is not False
    if variant.requires_shipping and spec.shipping_address is None:
        return "not quoted: it ships, and the file names no shipping_address"
    return CreateItemsQuoteRequest(
        email=spec.email,
        items=[QuoteItem(variant_id=variant.id, quantity=spec.quantity)],
        shipping_address=spec.shipping_address if ships else None,
    )


async def _quote(
    reap: ReapClient,
    request: CreateItemsQuoteRequest | CreateExternalCheckoutQuoteRequest,
    key: str,
) -> Quote | str:
    try:
        return await reap.create_quote(request, idempotency_key=key[:255])
    except (ReapError, ReapTransportError) as error:
        return _refused(error)


async def _search(
    reap: ReapClient, request: ProductSearchRequest
) -> tuple[ProductSearchRequest, ProductSearchResponse | str, str | None]:
    """Search once; ``MERCHANT_NOT_RESOLVED`` searches again without the preference.

    Args:
        reap: The Reap backend.
        request: The search.

    Returns:
        The request finally sent, the response or why it failed, and a note if the
        merchant preference was dropped.
    """
    try:
        return request, await reap.search_products(request), None
    except ReapError as error:
        if (
            error.code != AgenticErrorCode.MERCHANT_NOT_RESOLVED
            or request.merchant_preference is None
        ):
            return request, _refused(error), None
    except ReapTransportError as error:
        return request, _refused(error), None
    request = request.model_copy(update={"merchant_preference": None})
    note = "MERCHANT_NOT_RESOLVED: searched again without the merchant preference"
    try:
        return request, await reap.search_products(request), note
    except (ReapError, ReapTransportError) as error:
        return request, _refused(error), note


async def _expand(
    reap: ReapClient,
    summary: ProductSummary,
    detail: ProductDetail | None,
    *,
    need: Need | None,
    unjudged: bool,
    merchants: Sequence[str] | None,
) -> ProductRead:
    """Resolve and judge one product's candidates.

    Args:
        reap: The Reap backend.
        summary: The search result.
        detail: Its details, if they answered.
        need: The need to judge against; None for a free search.
        unjudged: Whether a value match goes unjudged for want of an estimate.
        merchants: The file's merchant scope; None for a free search.

    Returns:
        The product with its candidates.
    """
    if detail is None:
        return ProductRead(summary, None, (), "details did not answer for it")
    merchant = detail.merchant.name if detail.merchant else summary.merchant.name
    scope = None if merchants is None else in_scope(merchant, merchants)
    combinations = _option_sets(detail, need.spec.match if need else None)
    note = None
    resolved: list[Variant] = []
    if not combinations:
        note = "no option combination the match accepts; the default variant is judged"
        resolved.append(detail.default_variant)
    for option_ids in combinations:
        if not option_ids:
            resolved.append(detail.default_variant)
            continue
        try:
            resolved.append(
                await reap.resolve_variant(
                    ResolveVariantRequest(product_id=detail.id, option_ids=option_ids)
                )
            )
        except (ReapError, ReapTransportError) as error:
            note = f"a combination did not resolve: {_refused(error)}"
    candidates: list[Candidate] = []
    for raw in resolved:
        variant = CatalogueVariant.from_reap(raw, product_id=detail.id, merchant=merchant)
        if need is None:
            fills: bool | None = None
            judgement = "not judged: a free search has no match"
        else:
            verdict, judgement = judge_variant(need, variant, quantity=need.spec.quantity)
            fills = None if unjudged and verdict else verdict
            if fills is None:
                judgement = f"{judgement}; {_NO_ESTIMATE}"
        candidates.append(
            Candidate(
                product_id=detail.id,
                product=detail.name,
                merchant=merchant,
                in_scope=scope,
                variant=variant,
                raw=raw,
                fills=fills,
                judgement=judgement,
            )
        )
    return ProductRead(summary, detail, tuple(candidates), note)


async def _read(
    reap: ReapClient,
    label: str,
    request: ProductSearchRequest,
    *,
    need: Need | None,
    unjudged: bool = False,
    merchants: Sequence[str] | None = None,
) -> Probe:
    request, answer, note = await _search(reap, request)
    if isinstance(answer, str):
        return Probe(label, request, error=answer, note=note)
    ids = [p.id for p in answer.products[:DETAILS_LIMIT]]
    details: ProductDetailsResponse | None = None
    if ids:
        try:
            details = await reap.product_details(ProductDetailsRequest(product_ids=ids))
        except (ReapError, ReapTransportError) as error:
            note = f"details refused: {_refused(error)}"
    found = {d.id: d for d in details.products} if details else {}
    products = [
        await _expand(
            reap,
            summary,
            found.get(summary.id),
            need=need,
            unjudged=unjudged,
            merchants=merchants,
        )
        for summary in answer.products[:DETAILS_LIMIT]
    ]
    if len(answer.products) > DETAILS_LIMIT:
        note = f"only the first {DETAILS_LIMIT} of {len(answer.products)} products expanded"
    return Probe(label, request, answer, details, tuple(products), note=note)


async def probe_purchase(
    reap: ReapClient,
    purchase: PurchaseConfig,
    *,
    label: str,
    estimate_usd: Decimal | None,
    quote: bool,
    run_id: str,
) -> Probe:
    """Read the catalogue for one ``purchase:`` file.

    Args:
        reap: The Reap backend.
        purchase: The file's block.
        label: How the file is named in the output.
        estimate_usd: The live estimate, to judge a value match; None to value only.
        quote: Whether to land a quote for each candidate that passes rule 10.
        run_id: Names this run in each quote's idempotency key.

    Returns:
        The probe.
    """
    spec = purchase.need_spec()
    if spec.route == Route.CHECKOUT_URL:
        cart, address = spec.external_checkout, spec.shipping_address
        if not quote or cart is None or address is None:
            return Probe(
                label,
                None,
                note="the checkout_url route has nothing to search; --quote lands the cart's quote",
            )
        landed = await _quote(
            reap,
            CreateExternalCheckoutQuoteRequest(
                email=spec.email,
                external_checkout=ExternalCheckout(
                    merchant_domain=cart.merchant_domain, checkout_url=cart.checkout_url
                ),
                shipping_address=address,
            ),
            f"probe:{run_id}:url",
        )
        if isinstance(landed, str):
            return Probe(label, None, cart_error=landed)
        return Probe(label, None, cart_quote=landed)
    assert spec.search is not None
    need, unjudged = _need(spec, estimate_usd)
    probe = await _read(
        reap,
        label,
        spec.search.request(),
        need=need,
        unjudged=unjudged,
        merchants=purchase.merchants,
    )
    if not quote:
        return probe
    products: list[ProductRead] = []
    for product in probe.products:
        candidates: list[Candidate] = []
        for candidate in product.candidates:
            if candidate.fills is False or candidate.variant.available is False:
                candidates.append(candidate)
                continue
            request = _items_quote(spec, candidate.variant)
            landed = (
                request
                if isinstance(request, str)
                else await _quote(reap, request, f"probe:{run_id}:{candidate.variant.id}")
            )
            candidates.append(
                replace(candidate, quote_error=landed)
                if isinstance(landed, str)
                else replace(candidate, quote=landed)
            )
        products.append(replace(product, candidates=tuple(candidates)))
    return replace(probe, products=tuple(products))


async def probe_query(
    reap: ReapClient, query: str, *, country: str | None, currency: str | None, limit: int = 20
) -> Probe:
    """Run a free search, for a product named by hand.

    Args:
        reap: The Reap backend.
        query: Free text.
        country: The buyer's country, if any.
        currency: The pricing currency, if any.
        limit: Results per page.

    Returns:
        The probe; its candidates are each product's default or resolved variants,
        unjudged.
    """
    context = SearchPlace(country=country, currency=currency) if country or currency else None
    search = CatalogueSearch(query=query, context=context, limit=limit)
    return await _read(reap, f'free search "{query}"', search.request(), need=None)


def bom_purchases(path: Path, part: PurchaseConfig) -> list[tuple[str, PurchaseConfig]]:
    """Turn each part of a machine's bill of materials into the purchase it is bought under.

    The part's query, price band and accepted option values replace the part file's
    search query, price band and match; the merchants, context, address and grant stay
    the part file's.

    Args:
        path: The machine file.
        part: The part-shaped purchase file on the catalogue route.

    Returns:
        A label and a purchase block per part, in the file's order.

    Raises:
        ProbeError: If the file does not load, or the part file searches no catalogue.
    """
    try:
        machine = _MachineFile.model_validate(yaml.safe_load(path.read_text()))
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ProbeError(f"{path}: {error}") from error
    if part.search is None:
        raise ProbeError(f"{path}: the part file searches no catalogue, so its parts cannot be")
    found: list[tuple[str, PurchaseConfig]] = []
    for fault, entry in machine.bom.items():
        search = part.search.model_copy(update={"query": entry.query, "price": entry.price})
        purchase = part.model_copy(
            update={"search": search, "match": AttributeMatch(attributes=entry.accept)}
        )
        found.append((f"{shown_path(path)}: {fault}, {entry.part}", purchase))
    return found


def _part_purchase(settings: Settings) -> PurchaseConfig:
    """Return the purchase block the live session buys a part under.

    Args:
        settings: The settings naming the file in force.

    Returns:
        The file in force when it is shape ``part`` on the catalogue route, else the
        prepared ``purchase-part.yaml``.
    """
    try:
        in_force = load_purchase(purchase_config_path(settings))
    except (ScenarioError, OSError):
        in_force = None
    if in_force is not None and in_force.shape == Shape.PART and in_force.search is not None:
        return in_force
    return load_purchase(PURCHASE_CONFIG_DIR / "purchase-part.yaml")


def _machine(args: argparse.Namespace) -> Path | None:
    if args.machine is not None:
        return args.machine
    if args.config or args.query:
        return None
    path = _MachineSetting().machine_config
    return path if path.exists() else None


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'s' * (number != 1)}"


def _money(money: Money | None) -> str:
    return "-" if money is None else f"{money.currency} {money.amount:.2f}"


def _against(final: Money, grant: PurchaseGrant | None) -> str:
    if grant is None:
        return ""
    if final.currency != BUDGET_CURRENCY:
        return (
            f"; refused under this grant: priced in {final.currency}, the budget in "
            f"{BUDGET_CURRENCY}"
        )
    if final.amount > grant.per_purchase_usd:
        return f"; refused under this grant: above the per-purchase cap {grant.per_purchase_usd}"
    threshold = grant.approval_threshold_usd
    if threshold is not None and final.amount > threshold:
        return f"; escalated to the operator under this grant (above {threshold})"
    approval = f", approval above {threshold}" if threshold is not None else ""
    return f"; allowed under this grant (cap {grant.per_purchase_usd}{approval})"


def _quote_lines(quote: Quote, grant: PurchaseGrant | None) -> list[str]:
    parts = quote.amount_breakdown
    tax = parts.tax
    taxed = (
        "-" if tax is None else f"{_money(tax.amount)}{' incl.' if tax.included_in_prices else ''}"
    )
    figures = [
        f"items {_money(parts.items_subtotal)}",
        f"shipping {_money(parts.shipping)}",
        f"tax {taxed}",
        *(f"less {d.name} {_money(d.amount)}" for d in parts.discounts or []),
        *(f"{c.name} {_money(c.amount)}" for c in parts.additional_charges or []),
    ]
    lines = [
        f"      quote: final {_money(parts.final_amount)} ({', '.join(figures)}); "
        f"expires {quote.expires_at}{_against(parts.final_amount, grant)}"
    ]
    lines.extend(
        f"      shipping option {o.name}: {_money(o.price)}{' (selected)' if o.selected else ''}"
        for o in quote.shipping_options
    )
    return lines


def _verdict(candidate: Candidate) -> str:
    if candidate.fills is None:
        return candidate.judgement
    return f"rule 10: {'pass' if candidate.fills else 'FAIL'} ({candidate.judgement})"


def _render_cart(probe: Probe, purchase: PurchaseConfig | None) -> list[str]:
    lines: list[str] = []
    cart = purchase.external_checkout if purchase else None
    if probe.cart_quote is not None and cart is not None:
        final = _money(probe.cart_quote.amount_breakdown.final_amount)
        lines.append(f"  cart {cart.checkout_url}: final {final}")
        lines.extend(_quote_lines(probe.cart_quote, purchase.grant if purchase else None))
    elif probe.cart_error is not None:
        lines.append(f"  cart not quoted: {probe.cart_error}")
    return lines


def _render_product(product: ProductRead, grant: PurchaseGrant | None) -> list[str]:
    summary, candidates = product.summary, product.candidates
    scope = candidates[0].in_scope if candidates else None
    tag = "" if scope is None else (" [in scope]" if scope else " [not in the grant's merchants]")
    merchant = candidates[0].merchant if candidates else summary.merchant.name
    low, high = summary.price_range.min, summary.price_range.max
    prices = _money(low) if low == high else f"{_money(low)} to {_money(high)}"
    lines = [f"  {merchant}{tag}  {summary.name}  {prices}  {summary.id}"]
    for option in product.detail.options if product.detail else []:
        labels = ", ".join(
            f"{v.label}{' (sold out)' if v.available is False else ''}" for v in option.values
        )
        lines.append(f"    {option.name}: {labels}")
    if product.note:
        lines.append(f"    {product.note}")
    for candidate in candidates:
        variant = candidate.variant
        options = ", ".join(f"{k} {v}" for k, v in variant.options.items()) or "no options"
        stock = "" if variant.available is not False else ", sold out"
        ships = {True: ", ships", False: ", no shipping", None: ""}[variant.requires_shipping]
        lines.append(
            f"    {variant.id}  {_money(candidate.raw.price)}  {options}{stock}{ships}  "
            f"{_verdict(candidate)}"
        )
        if candidate.quote is not None:
            lines.extend(_quote_lines(candidate.quote, grant))
        elif candidate.quote_error is not None:
            lines.append(f"      quote: {candidate.quote_error}")
    return lines


def _summary(probe: Probe) -> str:
    every = [c for p in probe.products for c in p.candidates]
    passing = [c for c in every if c.fills]
    scoped = [c for c in passing if c.in_scope]
    unjudged = sum(c.fills is None for c in every)
    valued = f"; {unjudged} valued, not judged (no --estimate-usd)" if unjudged else ""
    return (
        f"Candidates passing rule 10: {len(passing)} of {len(every)}, {len(scoped)} from "
        f"the grant's merchants{valued}."
    )


def render(probe: Probe, purchase: PurchaseConfig | None) -> str:
    """Write one probe as the operator reads it.

    Args:
        probe: The probe.
        purchase: The file's block, for its grant; None for a free search.

    Returns:
        The text.
    """
    lines = [f"== {probe.label}"]
    request = probe.request
    if request is None:
        lines.extend(_render_cart(probe, purchase))
    elif probe.error is not None or probe.search is None:
        lines.append(f'Search "{request.query}"{_place(request)} refused: {probe.error}')
    else:
        results = probe.search.products
        merchants = {p.merchant.name for p in results}
        warnings = "; ".join(probe.search.warnings) or "none"
        lines.append(
            f'Search "{request.query}"{_place(request)}: {_count(len(results), "product")} '
            f"from {_count(len(merchants), 'merchant')}; warnings: {warnings}"
        )
        grant = purchase.grant if purchase else None
        for product in probe.products:
            lines.extend(_render_product(product, grant))
        if purchase is not None and any(p.candidates for p in probe.products):
            lines.append(_summary(probe))
    if probe.note:
        lines.append(f"  {probe.note}")
    return "\n".join(lines)


def _place(request: ProductSearchRequest) -> str:
    context = request.context
    return f" ({context.country}, {context.currency})" if context else ""


def _candidate_json(candidate: Candidate) -> dict[str, JsonValue]:
    return {
        "product_id": candidate.product_id,
        "merchant": candidate.merchant,
        "in_scope": candidate.in_scope,
        "variant": candidate.raw.to_wire(),
        "rule_10": {"fills": candidate.fills, "detail": candidate.judgement},
        "quote": candidate.quote.to_wire() if candidate.quote else None,
        "quote_error": candidate.quote_error,
    }


def snapshot(probes: Sequence[Probe], *, backend: str, out_dir: Path, taken_at: datetime) -> Path:
    """Write every raw answer to a dated JSON file.

    Args:
        probes: The probes.
        backend: ``mock`` or ``sandbox``.
        out_dir: The snapshot directory, created if missing.
        taken_at: When the probe ran, in UTC.

    Returns:
        The file written.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"probe-{taken_at.strftime('%Y%m%dT%H%M%SZ')}-{backend}.json"
    document: dict[str, JsonValue] = {
        "taken_at": taken_at.isoformat(),
        "backend": backend,
        "probes": [
            {
                "label": p.label,
                "request": p.request.to_wire() if p.request else None,
                "search": p.search.to_wire() if p.search else None,
                "details": p.details.to_wire() if p.details else None,
                "candidates": [_candidate_json(c) for r in p.products for c in r.candidates],
                "cart_quote": p.cart_quote.to_wire() if p.cart_quote else None,
                "cart_error": p.cart_error,
                "error": p.error,
                "note": p.note,
            }
            for p in probes
        ],
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return path


def _files(configs: Sequence[Path], queries: Sequence[str], settings: Settings) -> list[Path]:
    if configs:
        return list(configs)
    if queries:
        return []
    in_force = purchase_config_path(settings)
    shipped = [p.resolve() for p in SHIPPED]
    return [*SHIPPED, *([] if in_force.resolve() in shipped else [in_force])]


async def _run(
    settings: Settings, args: argparse.Namespace, files: Sequence[Path], run_id: str
) -> list[tuple[Probe, PurchaseConfig | None]]:
    reap = reap_for(settings)
    if reap is None:
        raise ProbeError(
            "Kwal needs a usable session (PWS_CREDENTIALS_FILE)"
            if settings.reap_backend == "kwal"
            else "the sandbox needs REAP_API_KEY in .env"
        )
    done: list[tuple[Probe, PurchaseConfig | None]] = []
    try:
        for path in files:
            label = shown_path(path)
            try:
                purchase = load_purchase(path)
            except (ScenarioError, OSError) as error:
                done.append((Probe(label, None, error=f"the file does not load: {error}"), None))
                continue
            label = f"{label}: shape {purchase.shape.value}, {purchase.route.value} route"
            probe = await probe_purchase(
                reap,
                purchase,
                label=label,
                estimate_usd=args.estimate_usd,
                quote=args.quote,
                run_id=run_id,
            )
            done.append((probe, purchase))
        machine = _machine(args)
        if machine is not None:
            try:
                parts = bom_purchases(machine, _part_purchase(settings))
            except (ProbeError, ScenarioError) as error:
                done.append((Probe(shown_path(machine), None, error=str(error)), None))
                parts = []
            for label, purchase in parts:
                probe = await probe_purchase(
                    reap,
                    purchase,
                    label=label,
                    estimate_usd=args.estimate_usd,
                    quote=args.quote,
                    run_id=run_id,
                )
                done.append((probe, purchase))
        for query in args.query:
            probe = await probe_query(reap, query, country=args.country, currency=args.currency)
            done.append((probe, None))
    finally:
        await reap.aclose()
    return done


def main(argv: list[str] | None = None) -> int:
    """Probe the catalogue, print it and snapshot it.

    Args:
        argv: Arguments, or the process's own.

    Returns:
        0 when every search answered, 1 when one did not, 2 when the settings cannot work.
    """
    parser = argparse.ArgumentParser(description="Read the Reap catalogue for the purchase files.")
    parser.add_argument("--config", type=Path, action="append", default=[], help="a purchase file")
    parser.add_argument("--query", action="append", default=[], help="a free search")
    parser.add_argument("--machine", type=Path, default=None, help="a machine file's parts")
    parser.add_argument("--country", default="SG", help="free searches' country")
    parser.add_argument("--currency", default="USD", help="free searches' currency")
    parser.add_argument("--quote", action="store_true", help="land each candidate's quote")
    parser.add_argument("--estimate-usd", type=Decimal, default=None, help="shape (a)'s estimate")
    parser.add_argument("--out", type=Path, default=SNAPSHOT_DIR, help="snapshot directory")
    parser.add_argument("--no-snapshot", action="store_true", help="print only")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        settings.check()
    except ConfigError as error:
        print(f"Not probing: {error}")
        return 2
    taken_at = utc_now()
    run_id = taken_at.strftime("%Y%m%dT%H%M%SZ")
    files = _files(args.config, args.query, settings)
    mode = "read-only (search, details, variant)"
    if args.quote:
        mode += " and quotes (no checkout)"
    stamp = taken_at.isoformat(timespec="seconds")
    print(f"Catalogue probe on {settings.reap_backend}: {mode}, {stamp}")
    done = asyncio.run(_run(settings, args, files, run_id))
    for probe, purchase in done:
        print()
        print(render(probe, purchase))
    if not args.no_snapshot:
        path = snapshot(
            [p for p, _ in done], backend=settings.reap_backend, out_dir=args.out, taken_at=taken_at
        )
        print()
        print(f"Snapshot: {path}")
    return 1 if any(p.error for p, _ in done) else 0


if __name__ == "__main__":
    raise SystemExit(main())
