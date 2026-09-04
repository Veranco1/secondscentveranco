"""Lightweight, dict-based i18n for the public-facing pages (header, footer,
homepage). Deliberately small in scope: this is not a general i18n framework,
just enough to let a visitor switch the marketing-facing chrome between
Dutch (default) and English, including the footer, which previously stayed
Dutch regardless of what a visitor picked.

Account/dashboard/checkout/admin pages remain Dutch-only for now — extending
`STRINGS` with more keys and wrapping more templates in `t(...)` is all that
is needed to widen coverage later.
"""

from flask import session

SUPPORTED_LANGUAGES = ("nl", "en")
DEFAULT_LANGUAGE = "nl"

STRINGS = {
    "nl": {
        "nav_discover": "Ontdekken",
        "nav_how": "Hoe het werkt",
        "nav_sell": "Verkopen",
        "nav_my_purchases": "Mijn aankopen",
        "nav_my_listings": "Mijn advertenties",
        "nav_admin": "Beheer",
        "sell_now": "Verkoop nu",
        "login": "Inloggen",
        "logout": "Uitloggen",

        "trust_1": "Elke fles gecontroleerd op echtheid",
        "trust_2": "Kopersbescherming op elke bestelling",
        "trust_3": "4.8 gemiddelde beoordeling",
        "trust_4": "Gratis adverteren, geen kosten",

        "hero_eyebrow": "Tweedehands parfum, gecontroleerd",
        "hero_h1": "Nieuwe geur. Tweede liefde.",
        "hero_p": "Koop en verkoop gecontroleerde parfum, decants en verouderde flessen, van mensen die ze echt droegen, niet uit een magazijn.",
        "hero_cta_buy": "Nu kopen",
        "hero_cta_sell": "Verkoop een fles",
        "hero_stat1_num": "12.400+",
        "hero_stat1_label": "flessen doorgegeven",
        "hero_stat2_num": "100%",
        "hero_stat2_label": "echtheidscontrole",
        "hero_stat3_num": "9.200",
        "hero_stat3_label": "reviews",

        "how_h2": "Van andermans kaptafel tot jouw deur",
        "how_p": "Geen magazijnen, geen mysterieuze voorraad: elke fles komt uit een echte collectie en doorloopt een echtheidscontrole voordat hij verzonden wordt.",
        "step1_title": "Plaats wat je niet meer gebruikt",
        "step1_p": "Fotografeer de fles volgens de 12 vaste hoeken, geef het vulniveau op en bepaal je prijs.",
        "step2_title": "Wij controleren de echtheid",
        "step2_p": "Batchcodes, verpakking en foto's worden automatisch en waar nodig handmatig beoordeeld voordat een advertentie live gaat.",
        "step3_title": "Veilig kopen en verkopen",
        "step3_p": "Kopers betalen veilig via kopersbescherming, verkopers verzenden binnen de afgesproken termijn, en de betaling wordt pas vrijgegeven na bevestiging.",

        "collection_h2": "Ontdek de collectie",
        "collection_p": "Flessen geplaatst door mensen zelf, geprijsd en gefotografeerd door de verkoper. Geen magazijn.",
        "search_placeholder": "Zoek merk of geur…",
        "filter_all_conditions": "Alle staten",
        "search_btn": "Zoeken",
        "card_view": "Bekijk advertentie",
        "card_meta": "{size} ml · {percent}% gevuld",

        "empty_title": "Nog geen advertenties gevonden",
        "empty_p": "Pas je zoekopdracht aan, of",
        "empty_cta": "plaats de eerste advertentie",

        "cta_eyebrow": "Te goed om te laten staan",
        "cta_h2": "Jouw kast is iemands verlanglijstje.",
        "cta_p": "Verkoop je parfum of sta open voor een ruil. Plaatsen is gratis.",
        "cta_btn": "Geef je parfum door",

        "footer_tagline": "De marktplaats voor tweedehands parfum tussen mensen. Elke fles wordt gecontroleerd voordat hij verzonden wordt.",
        "footer_col_marketplace": "Marktplaats",
        "footer_col_account": "Account",
        "footer_col_trust": "Vertrouwen",
        "footer_trust_text": "Echtheidscontrole en kopersbescherming op elke bestelling. Zie de statuslegenda op elke advertentie.",
        "footer_account_create": "Account aanmaken",
        "footer_copyright": "© SecondScent",
        "footer_demo_note": "Demo-omgeving: betalingen lopen via een gesimuleerde Stripe-koppeling, geen echt geld.",
    },
    "en": {
        "nav_discover": "Discover",
        "nav_how": "How it works",
        "nav_sell": "Sell",
        "nav_my_purchases": "My purchases",
        "nav_my_listings": "My listings",
        "nav_admin": "Admin",
        "sell_now": "Sell now",
        "login": "Log in",
        "logout": "Log out",

        "trust_1": "Every bottle authenticity-checked",
        "trust_2": "Buyer protection on every order",
        "trust_3": "4.8 average rating",
        "trust_4": "Free to list, no fees",

        "hero_eyebrow": "Pre-loved fragrance, verified",
        "hero_h1": "New scent. Second love.",
        "hero_p": "Buy and sell authenticated perfume, decants, and discontinued bottles, from people who actually wore them, not warehouses.",
        "hero_cta_buy": "Buy now",
        "hero_cta_sell": "Sell a bottle",
        "hero_stat1_num": "12,400+",
        "hero_stat1_label": "bottles rehomed",
        "hero_stat2_num": "100%",
        "hero_stat2_label": "authenticity check",
        "hero_stat3_num": "9,200",
        "hero_stat3_label": "reviews",

        "how_h2": "From someone's vanity to your doorstep",
        "how_p": "No warehouses, no mystery stock: every bottle comes from a real collection and passes an authenticity check before it ships.",
        "step1_title": "List what you've outgrown",
        "step1_p": "Photograph the bottle from the 12 fixed angles, note the fill level, and set your price.",
        "step2_title": "We check it's genuine",
        "step2_p": "Batch codes, packaging, and photos are reviewed automatically, and by hand where needed, before a listing goes live.",
        "step3_title": "Buy and sell safely",
        "step3_p": "Buyers pay through buyer protection, sellers ship within the agreed window, and payment is only released after confirmation.",

        "collection_h2": "Discover the collection",
        "collection_p": "Bottles listed by real people, priced and photographed by the seller. No warehouse.",
        "search_placeholder": "Search brand or scent…",
        "filter_all_conditions": "All conditions",
        "search_btn": "Search",
        "card_view": "View listing",
        "card_meta": "{size} ml · {percent}% full",

        "empty_title": "No listings found yet",
        "empty_p": "Adjust your search, or",
        "empty_cta": "post the first listing",

        "cta_eyebrow": "Too good to leave sitting",
        "cta_h2": "Someone's wishlist is in your cabinet.",
        "cta_p": "Sell your perfume or stay open to a trade. Listing is always free.",
        "cta_btn": "Pass your perfume on",

        "footer_tagline": "The marketplace for pre-loved perfume, person to person. Every bottle is checked before it ships.",
        "footer_col_marketplace": "Marketplace",
        "footer_col_account": "Account",
        "footer_col_trust": "Trust",
        "footer_trust_text": "Authenticity checks and buyer protection on every order. See the status legend on each listing.",
        "footer_account_create": "Create an account",
        "footer_copyright": "© SecondScent",
        "footer_demo_note": "Demo environment: payments run through a simulated Stripe connection, no real money moves.",
    },
}


def get_lang():
    lang = session.get("lang", DEFAULT_LANGUAGE)
    return lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def t(key, **kwargs):
    lang = get_lang()
    text = STRINGS.get(lang, {}).get(key)
    if text is None:
        text = STRINGS[DEFAULT_LANGUAGE].get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text
