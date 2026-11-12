"""Minimal HTML pages for signed wallet ops links (no external assets)."""

from __future__ import annotations

import html
from typing import Optional


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:40rem;"
        "margin:2rem auto;padding:0 1rem;line-height:1.45;color:#111}"
        "label{display:block;font-weight:600;margin:1rem 0 .35rem}"
        "input,textarea{width:100%;box-sizing:border-box;padding:.55rem .65rem;"
        "font:inherit;border:1px solid #bbb;border-radius:6px}"
        "button{margin-top:1.25rem;padding:.65rem 1.1rem;font:inherit;font-weight:600;"
        "border:0;border-radius:6px;background:#111;color:#fff;cursor:pointer}"
        ".meta{background:#f6f6f6;border-radius:8px;padding:.85rem 1rem;margin:1rem 0}"
        ".meta div{margin:.2rem 0}.err{color:#a40000}.ok{color:#0a5}"
        "</style></head><body>"
        f"{body}</body></html>"
    )


def error_page(title: str, message: str) -> str:
    return _page(
        title,
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"err\">{html.escape(message)}</p>",
    )


def success_page(title: str, message: str) -> str:
    return _page(
        title,
        f"<h1>{html.escape(title)}</h1>"
        f"<p class=\"ok\">{html.escape(message)}</p>",
    )


def form_page(
    *,
    action: str,
    withdrawal_id: str,
    amount_label: str,
    vendor_name: str,
    masked_account: str,
    token: str,
    form_action_url: str,
    error: Optional[str] = None,
) -> str:
    is_paid = action == "paid"
    heading = "Mark withdrawal paid" if is_paid else "Reject withdrawal"
    field_label = "Bank UTR" if is_paid else "Rejection reason (shown to vendor)"
    field_hint = (
        "Enter the UTR from the bank transfer."
        if is_paid
        else "At least 3 characters."
    )
    err_html = (
        f"<p class=\"err\">{html.escape(error)}</p>" if error else ""
    )
    if is_paid:
        control = (
            "<input id=\"field\" name=\"utr\" required minlength=\"6\" maxlength=\"40\" "
            "pattern=\"[A-Za-z0-9]+\" autocomplete=\"off\">"
        )
    else:
        control = (
            "<textarea id=\"field\" name=\"reason\" required "
            "minlength=\"3\" maxlength=\"255\" rows=\"3\"></textarea>"
        )
    body = (
        f"<h1>{html.escape(heading)}</h1>"
        f"{err_html}"
        "<div class=\"meta\">"
        f"<div><strong>Withdrawal</strong> {html.escape(withdrawal_id)}</div>"
        f"<div><strong>Amount</strong> {html.escape(amount_label)}</div>"
        f"<div><strong>Vendor</strong> {html.escape(vendor_name or '—')}</div>"
        f"<div><strong>Account</strong> {html.escape(masked_account or '—')}</div>"
        "</div>"
        f"<form method=\"post\" action=\"{html.escape(form_action_url)}\">"
        f"<input type=\"hidden\" name=\"t\" value=\"{html.escape(token)}\">"
        f"<label for=\"field\">{html.escape(field_label)}</label>"
        f"{control}"
        f"<p>{html.escape(field_hint)}</p>"
        f"<button type=\"submit\">{'Confirm paid' if is_paid else 'Confirm reject'}</button>"
        "</form>"
    )
    return _page(heading, body)
