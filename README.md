# SecondScent Platform

De volledige, werkende SecondScent-website: registreren/inloggen, een
advertentie plaatsen met de 12 bewijsfoto's en echtheidscontrole, browsen en
kopen met een echte checkout (kopersbescherming inbegrepen), een koper- en
verkopersdashboard, geschillen, en een adminomgeving — allemaal echt gekoppeld
aan de backend hieronder (geen losse mockup meer met nepdata). De statische
mockup (`secondscent.html`, apart geleverd) is de bron van de huisstijl
(kleuren, typografie, componenten) die deze site overneemt; die mockup zelf is
niet aangepast — zie **"De website"** verderop voor hoe dat precies werkt en
welke scope-keuzes daarbij gemaakt zijn.

**Lees eerst [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).** Dat document
bevat de volledige analyse, architectuur, database-ontwerp, betaal-architectuur
(Stripe Connect, met bronvermelding naar de actuele officiële documentatie) en
het gefaseerde implementatieplan waar dit project vandaan komt.

## Status

| Fase | Status |
|---|---|
| 0 — Fundament (database-schema + statusmachine) | ✅ Gebouwd én getest tegen een echte PostgreSQL 16-instantie |
| 1 — Accounts & auth | ✅ Gebouwd én getest (16/16 tests slagen, zie hieronder) |
| Buyer Protection (checkout, betalingen, kopersbescherming, disputes, admin fee-configuratie) | ✅ Gebouwd én getest (68/68 checks slagen, alle 14 gevraagde scenario's + extra's — zie hieronder) |
| Authenticity & Anti-Counterfeit (advertentie + 12 bewijsfoto's, risk engine, verificatiestatussen, manual review, counterfeit-melding na aankoop, admin intelligence) | ✅ Gebouwd én getest (82/82 checks slagen — zie hieronder) |
| **De website** (server-gerenderde pagina's bovenop alle bovenstaande JSON-API's: browsen/kopen, verkopen + foto-upload, checkout, koper-/verkopersdashboard, geschillen, adminomgeving) | ✅ Gebouwd én getest (zie "De website" hieronder) |
| Notificatie-bezorging (e-mail/push — nu alleen interne `notifications`-tabel), advertenties bewerken/verwijderen/zoeken op meer dan merk+naam+staat | Nog te bouwen — zie `docs/ARCHITECTURE.md` § F |

## Fase 0 — database

`db/schema.sql` is de bron van waarheid: volledige PostgreSQL-schema met
constraints, indexes, en een **database-trigger die de orderstatusmachine
afdwingt** (een ongeldige statusovergang wordt geweigerd door Postgres zelf,
niet alleen door applicatiecode). Toepassen op een lege Postgres-database:

```bash
createdb secondscent
psql -d secondscent -f db/schema.sql
```

Dit is al één keer end-to-end getest: een geldige overgang
(`payment_pending → paid → awaiting_shipment`) lukt, een overgang die de
inspectieperiode overslaat (`awaiting_shipment → completed`) wordt geweigerd
met een duidelijke foutmelding, de `chk_total_matches`-constraint weigert een
order waarvan de deelbedragen niet optellen tot het totaal, en een review kan
niet tweemaal op dezelfde order geplaatst worden en niet op jezelf.

## Fase 1 — accounts & auth

Een werkende Flask-blueprint (`app/auth/`) met registratie, login, logout,
sessies, CSRF-bescherming, rate limiting op inloggen, en een koper/verkoper-
rolvlag. Zie de code-comments in `app/auth/routes.py` voor de beveiligings-
keuzes en waarom.

**Belangrijk over de database in déze fase:** omdat deze ontwikkelomgeving
geen toegang heeft tot nieuwe Python-pakketten (geen Postgres-driver te
installeren), draait Fase 1 hier tegen een kleine sqlite3-stand-in
(`app/db.py`) die dezelfde vorm heeft als de `users`-tabel in
`db/schema.sql`. Zodra je dit in een omgeving met normale internettoegang
verder bouwt: installeer `requirements.txt` (die bevat ook `psycopg`,
`SQLAlchemy`, `stripe`, …) en vervang `app/db.py` door een echte
Postgres-verbinding naar het schema in `db/schema.sql` — dat is een
kwestie van de connectie omwisselen, niet van de tabellen herontwerpen.

### Zelf draaien

```bash
pip install -r requirements.txt   # Flask/Werkzeug is genoeg voor Fase 1
python3 run.py                    # http://127.0.0.1:5001
```

### Testen

```bash
python3 tests/test_auth.py
```

Dit dekt: registratie, dubbele e-mail geweigerd, zwak wachtwoord geweigerd,
CSRF-afdwinging, login met correct/onjuist wachtwoord, rate limiting na 5
mislukte pogingen, sessie vereist voor `/auth/me`, logout wist de sessie
daadwerkelijk, en de koper/verkoper-rolvlag raakt alleen het eigen account.
Alle 16 checks slagen.

## Buyer Protection

Het volledige kopersbeschermingssysteem: server-side checkout-prijsberekening,
een betaalarchitectuur op basis van Stripe Connect ("Separate charges and
transfers", zie `docs/ARCHITECTURE.md` § D), een 13-statussen orderstatus-
machine, een controleperiode na levering, een probleem-meldsysteem met 6
categorieën, een volledig dispute-systeem (bewijs/berichten/interne notities/
tijdlijn, alles append-only), en een admin-configureerbare fee-structuur.

### Wat er nu werkt

**Betalingen (`app/payments/stripe_client.py`)** — een `LiveStripeClient`
(echte Stripe SDK, voor productie met jouw eigen Stripe-account — zie hieronder)
en een `FakeStripeClient` (in-memory simulatie, inclusief idempotency-caching
en over-refund/over-transfer-bescherming) achter één interface. Welke actief
is, bepaalt `PAYMENTS_MODE` (env var, default `fake`). Zonder een echt
Stripe-account draait hier dus alles — checkout, refunds, payouts, webhooks —
end-to-end te testen met de fake client.

**Checkout & orders (`app/orders/`)** — `POST /orders/checkout` berekent
`item_price` / `shipping_cost` / `buyer_protection_fee` / `tax` / `total`
altijd server-side uit `listings` + de admin-configureerbare `fee_rules` en
`platform_config` — een door de client meegestuurde prijs wordt genegeerd
(getest, zie scenario 14 hieronder). De orderstatusmachine
(`app/orders/state_machine.py`) staat alléén de overgangen toe die in
`order_status_transitions` staan (dezelfde whitelist als de Postgres-trigger
in `db/schema.sql`) en schrijft elke overgang weg naar `order_status_history`.

**Disputes (`app/disputes/`)** — bewijs, berichten en (alleen voor admins)
interne notities per zaak, allemaal append-only afgedwongen door de database
zelf (zie de `trg_..._no_update`/`no_delete`-triggers in `app/db.py`, en de
overeenkomstige Postgres-triggers in `db/schema.sql`) — geen partij kan het
bewijs van de ander (of zichzelf) achteraf aanpassen of verwijderen, ook niet
via een bug in de applicatiecode. Admin lost een zaak op via
`POST /disputes/<id>/resolve` met `release_to_seller`, `refund_full`,
`refund_partial` of `require_return`.

**Achtergrondtaken (`app/jobs/sweep.py`)** — twee sweeps die ook zonder
gebruikersactie moeten gebeuren: een verkoper die niet verzendt vóór de
verzenddeadline → automatisch geannuleerd + volledig terugbetaald; een koper
die tijdens de controleperiode niets doet en geen geschil heeft geopend →
automatisch afgerond + uitbetaald aan de verkoper. Er is in deze sandbox geen
scheduler beschikbaar (zie hieronder) — `POST /admin/sweep` triggert ze
handmatig; in productie draaien ze op een cron/Celery-beat-achtige scheduler.

**Webhooks (`app/payments/webhooks.py`)** — `POST /webhooks/stripe` verifieert
de handtekening, is idempotent bij dubbele bezorging (via de UNIQUE
`stripe_event_id` in `webhook_events` — Stripe levert berichten "at least
once"), en verwerkt `payment_intent.succeeded`, `payment_intent.payment_failed`
en `charge.dispute.created` (chargebacks — bewust losgekoppeld van de gewone
orderstatusmachine, omdat een chargeback tot ~120 dagen na de betaling kan
binnenkomen, ook op een allang afgeronde order — zie de moduledocstring in
`app/payments/stripe_client.py`).

**Admin (`app/admin/`)** — fee-regels en platform-instellingen (verzendtarief,
belastingtarief, duur controleperiode/verzenddeadline/reactietermijn geschil)
aanpasbaar zonder codewijziging, met een append-only audit trail
(`fee_rule_history`).

### Testen

```bash
python3 tests/test_buyer_protection.py
```

Dekt, end-to-end tegen de echte Flask-app + een echte (tijdelijke) SQLite-db
+ de `FakeStripeClient`, alle 14 door jou expliciet gevraagde scenario's:
succesvolle order, betaling mislukt, verkoper verzendt niet (sweep), pakket
kwijt, succesvolle levering, koper bevestigt, controleperiode verloopt zonder
geschil (sweep), geschil vóór uitbetaling, refund, gedeeltelijke refund,
chargeback (zowel op een lopende als op een al afgeronde order), dubbele
webhook-bezorging, dubbel refund-verzoek, en een gemanipuleerde
frontend-prijs — plus autorisatie-checks (alleen de juiste koper/verkoper/
admin mag iets), en de append-only-afdwinging van bewijs op databaseniveau.
**68 van de 68 checks slagen.**

### Nog niet gebouwd / bewuste beperkingen

- **Echte Stripe-koppeling**: vereist jouw eigen Stripe-account — dat kan
  alleen jijzelf aanmaken (zie `docs/ARCHITECTURE.md` § D voor de exacte
  stappen en de geraadpleegde officiële documentatie). Zet daarna
  `PAYMENTS_MODE=live` + de twee sleutels in `.env`; de rest van de code
  verandert niet (dezelfde interface).
- **Carrier-tracking**: "na carrier-bevestigde levering start de
  controleperiode" is er als `POST /orders/<id>/mark-delivered`, bewust
  alleen voor admins aanroepbaar (geen van beide partijen kan een levering
  faken) — een echte koppeling met PostNL/DHL/etc. z'n trackingwebhook is
  een latere fase.
- **Scheduler**: de twee sweeps bestaan en zijn getest, maar draaien hier
  nog niet automatisch op een klok (geen scheduler in deze sandbox
  beschikbaar) — alleen handmatig via `POST /admin/sweep`.
- **Notificatie-bezorging**: gebeurtenissen worden weggeschreven in de
  `notifications`-tabel (in-app), maar er is nog geen e-mail/push-bezorging
  gebouwd.
- **Advertenties**: er is alleen een minimale `listings`-tabel om tegen af te
  rekenen — de volledige advertentie-CRUD, foto's en moderatie zijn expliciet
  uitgesteld ("advertenties komen later").

## Authenticity & Anti-Counterfeit

Volledig ontwerp in [`docs/AUTHENTICITY_ARCHITECTURE.md`](docs/AUTHENTICITY_ARCHITECTURE.md).
**Leidend principe, letterlijk in de code afgedwongen**: geen enkel
automatisch signaal — AI/beeldherkenning, batchcode, of verkoperinformatie —
bewijst ooit alleen dat een parfum 100% echt is. Automatische systemen doen
uitsluitend risico-inschatting en inconsistentiedetectie; de
`SecondScent Verified`-badge komt alleen tot stand via een expliciete
admin-beoordeling volgens een vaste standaard.

### Wat er nu werkt

**Advertentie + 12 bewijsfoto's (`app/listings/`)** — `POST /listings`
(minimaal nodig voor deze feature; volledige listings-CRUD is een latere
fase) en `POST /listings/<id>/photos` voor de 12 categorieën (voorkant/
achterkant/onderkant fles, verstuiver, dop, doos voorkant/achterkant/
onderkant, batchcode fles/verpakking, barcode, aankoopbewijs) met de vaste
uploadinstructies (`GET /listings/photo-categories`). Batchcodes worden
door de verkoper zelf overgetypt bij de foto — geen OCR-claim, zie
§ D.1 in het ontwerp.

**Echte beeldanalyse, geen black-box AI (`app/authenticity/photo_integrity.py`)**
— SHA-256 (exacte duplicaten), een difference-hash voor perceptuele
gelijkenis (ook na crop/hercompressie), EXIF waar aanwezig (GPS wordt
nooit opgeslagen, alleen of het aanwezig was), Laplacian-variance voor
wazigheid, en een Error Level Analysis-achtige check als zwak
manipulatiesignaal. Draait volledig lokaal op Pillow/NumPy — geen externe
AI-API, dus ook geen ongefundeerde "AI heeft dit geverifieerd"-claim
mogelijk. Alles hieronder is getest tegen echt gegenereerde
testafbeeldingen (duplicaten, wazige versies, hercomprimeerde
near-duplicates, een gesplitste foto voor de ELA-check) — niet
gesimuleerd.

**Risk engine (`app/authenticity/risk_engine.py`)** — 11 regelgebaseerde
signalen (ontbrekende verplichte foto, batchcode-mismatch, doos-
inconsistentie, foto-hergebruik binnen/tussen accounts, prijs ver onder
referentie, nieuw account + hoge waarde, ongebruikelijk veel identieke
listings kort na elkaar, eerdere bevestigde counterfeit-melding, wazige
of mogelijk gemanipuleerde bewijsfoto) tellen op tot een score → band
(low/medium/high). **De regels en gewichten worden nooit aan een koper of
verkoper getoond** — alleen `verification_status` en de vaste, eerlijke
uitleg daarbij; alleen admins zien de losse signalen.

**Verificatiestatussen (`app/authenticity/verification.py`)** —
`unverified` → `automated_checks_completed` (low risk, gewoon
gepubliceerd, expliciet **geen** garantie) / `additional_verification_required`
(medium risk, of verplichte foto's ontbreken — kan al zichtbaar zijn) /
`manual_review` (high risk, **niet** gepubliceerd tot een admin oordeelt)
→ `secondscent_verified` (alleen na expliciete admin-goedkeuring volgens
de vaste standaard in § F.1) of `rejected`. Wijzigt een verkoper na
verificatie een kernveld (merk, naam, inhoud, batchcode, doos) of
vervangt hij een verplichte foto, dan **vervalt de badge automatisch**
terug naar `manual_review` — geen enkel codepad behoudt hem stilzwijgend.

**Manual review dashboard (`app/admin/routes.py`)** —
`GET /admin/listings/review-queue`, de volledige zaak via
`GET /admin/listings/<id>/review` (foto's, verkopersgeschiedenis, risk-
signalen, vergelijkbare listings, eerdere meldingen, leesbare risk
summary), en de vier acties `approve` / `request_more_evidence` /
`reject` / `escalate` — elke actie gelogd in zowel de append-only
`listing_review_actions` als de bestaande `audit_logs`.

**Counterfeit-melding na aankoop (`POST /orders/<id>/report-counterfeit`)**
— hergebruikt de bestaande dispute-machinery (geen dubbele refund-logica),
vraagt gerichte bewijscategorieën (fles, bodem, batchcode, doos, nozzle,
verpakking, beschrijving van de afwijking), en admins krijgen een
side-by-side vergelijking met de oorspronkelijke advertentie-foto's via
`GET /admin/authenticity-reports/<id>`.

**Admin intelligence-dashboard** — `GET /admin/intelligence/signal-summary`,
`/flagged-sellers` (alleen id + e-mail, geen extra persoonsgegevens) en
`/photo-reuse-clusters` (dezelfde foto-hash bij meerdere verkopers — de
kern van cross-account patroonherkenning, bewust **zonder** IP/device-
tracking toe te voegen, zie § E in het ontwerp voor waarom).

**Privacy** — bewijsfoto's staan in een privé bestandsopslag
(`app/evidence_store.py`, lokaal in deze sandbox, met een expliciete
productienotitie om over te stappen op privé object storage) en zijn
alleen op te vragen via een geautoriseerde endpoint; aankoopbewijs is
extra beperkt tot eigenaar + admin; alle append-only tabellen
(risk_signals, risk_assessments, listing_verifications,
listing_review_actions) zijn op databaseniveau onveranderlijk, net als
het bestaande dispute-systeem.

### Testen

```bash
python3 tests/test_authenticity.py
```

Dekt, tegen de echte Flask-app + een echte (tijdelijke) SQLite-db + echte
Pillow/NumPy-beeldanalyse: elk van de 11 risk-signalen daadwerkelijk
getriggerd (niet alleen unit-getest), de volledige verificatie-levenscyclus
inclusief het automatisch vervallen na wijziging, alle 4 review-acties, de
counterfeit-meldflow met de admin side-by-side vergelijking, en een reeks
privacy-/autorisatiechecks (aankoopbewijs alleen voor eigenaar/admin,
batchcode/barcode/risicogegevens nooit zichtbaar voor een niet-eigenaar,
en een letterlijke scan dat nergens een "100% echt gegarandeerd"-achtige
claim voorkomt). **82 van de 82 checks slagen.**

### Nog niet gebouwd / bewuste beperkingen

- **OCR op batchcodes/barcodes**: de verkoper typt de code zelf over bij
  de foto; automatisch uitlezen zou een AI-claim zijn die deze omgeving
  niet kan onderbouwen.
- **Automatische documentredactie** (bijv. een creditcardnummer op een
  aankoopbewijs afdekken): vereist betrouwbare OCR + inpainting — bewust
  niet gebouwd, toegangsbeperking is het echte vangnet.
- **IP/device-fingerprinting** voor cross-account clustering: bewust niet
  toegevoegd — die data wordt nergens anders verzameld en het zou een
  nieuwe, invasieve trackinglaag zijn zonder juridisch kader.
- **Object storage**: bewijsfoto's staan nu op lokale disk; productie
  vervangt dit door S3 (of gelijkwaardig) met signed URLs — zelfde
  "wissel de implementatie, niet de aanroepers"-patroon als bij de
  sqlite3-devlaag en de Stripe-client.
- **Referentieprijzen**: `reference_prices` is een kale, admin-gevulde
  tabel — geen live marktdata-integratie.

## De website

Dit is de laag die alle bovenstaande, los geteste JSON-API's daadwerkelijk tot
een klikbare website maakt: `app/web/` (Flask-blueprint, alleen leesroutes —
zie hieronder waarom) + `app/templates/` (Jinja2) + `app/static/`
(huisstijl-CSS overgenomen uit `secondscent.html`, plus de echte logo's/
productfoto's).

### Hoe het is opgebouwd

- **`app/web/routes.py` schrijft nooit naar de database.** Elke actie die
  state verandert (registreren, een advertentie aanmaken, foto's uploaden,
  afrekenen, verzenden, ontvangst bevestigen, een geschil openen, een
  admin-beslissing) gebeurt vanuit de browser met `fetch()`
  (`app/static/js/app.js`) rechtstreeks naar de al bestaande, al geteste
  JSON-blueprints (`app.auth`, `app.listings`, `app.orders`, `app.disputes`,
  `app.admin`) — inclusief dezelfde `X-CSRF-Token`-header-eis. De
  webroutes lezen alleen de database om een pagina te renderen. Dat
  betekent: niets in deze laag kan afwijken van het gedrag dat de 166
  bestaande tests al dekken, en een admin-actie op de website is exact
  dezelfde aanroep als in `tests/test_authenticity.py`.
- **Huisstijl**: kleuren (`--cream`/`--green`/`--orange`/…), Fraunces +
  Work Sans, en de header/knoppen/kaarten/footer-componenten in
  `app/static/css/main.css` zijn overgenomen uit `secondscent.html`. De
  logo's, herofoto en vier voorbeeld-productfoto's uit die mockup zijn als
  echte, geoptimaliseerde bestanden meegenomen naar `app/static/img/` —
  verder gebruiken advertentiekaarten en de detailpagina de **echte**
  geüploade bewijsfoto's van een listing (via `/listings/<id>/photos/<id>/file`),
  niet de vaste mockup-plaatjes.
- **Statusbadges** (`app/templates/macros.html`) vertalen elke
  `verification_status`/order-status/dispute-status naar een kleur + de
  exacte NL-uitleg uit `STATUS_EXPLANATIONS` — nooit een eigen,
  ongecontroleerde claim over echtheid.

### Scope-keuze: alleen Nederlands

`secondscent.html` had marketing-copy in vijf talen via een client-side
i18n-script. Dit hele project is verder overal Nederlandstalig (elke
foutmelding, elke statusuitleg, elk formulierlabel in de backend). Vertaalde
advertenties, geschiltoelichtingen en dashboards bouwen is een wezenlijk
grotere, aparte feature dan "de huisstijl overnemen" — dus is bewust gekozen
voor een Nederlandstalige site, consistent met de rest van het platform.

### Twee bewuste dev-only stand-ins (nooit in productie)

Zonder een echte Stripe-koppeling (zie `app/payments/stripe_client.py`) zou
geen enkele bestelling in de browser ooit verder komen dan "wacht op
betaling", en zou "ontvangst bevestigen" altijd falen omdat een verkoper
nooit een voltooide Stripe Connect-onboarding kan hebben. Om de kernbelofte
van dit platform (kopersbescherming, van betaling tot uitbetaling) toch
end-to-end klikbaar te maken zijn er twee expliciet gelabelde, alleen-bij-
`PAYMENTS_MODE=fake`-werkende routes toegevoegd:

- `POST /orders/<id>/dev-pay` — zet de `FakeStripeClient`-PaymentIntent op
  geslaagd en draait vervolgens **exact dezelfde handler**
  (`_handle_payment_succeeded`) die de echte, ondertekende Stripe-webhook
  ook zou draaien. Geen nieuwe logica, geen omweg.
- `POST /auth/dev-enable-payouts` — simuleert een afgeronde Stripe
  Connect-koppeling voor de ingelogde verkoper (zichtbaar, gelabeld als
  simulatie, in het verkopersdashboard).

Beide weigeren met 403 zodra `PAYMENTS_MODE=live` staat, dus dit kan nooit
per ongeluk in een echte deploy actief blijven.

### Rondlopen

```bash
pip install -r requirements.txt   # Flask/Werkzeug/Pillow/numpy is genoeg
python3 run.py                    # http://127.0.0.1:5001
```

Doorloop als test: `/registreren` → verkoper worden → `/verkopen` een
advertentie aanmaken → de 12 foto's uploaden op `/verkopen/<id>` → indienen
voor controle → (bij lage/gemiddeld risico direct live, bij hoog risico
verschijnt hij in `/beheer` voor een tweede account met `is_admin=1`) →
met een derde account (koper) afrekenen op `/afrekenen/<id>` → betaling
simuleren → verkoper verzendt op de orderpagina → admin markeert "bezorgd" →
koper bevestigt ontvangst (verkoper moet eerst wel eenmalig de
Stripe Connect-simulatie aanzetten in `/verkopers/dashboard`).

### Testen

```bash
python3 tests/test_website_flows_1.py   # volledige happy-path levenscyclus
python3 tests/test_website_flows_2.py   # hoog-risico -> manual_review -> extra bewijs -> hernieuwde beoordeling
```

Beide scripts drijven de website aan via de Flask test-client — echte
HTTP-requests tegen zowel de pagina's (`GET`) als de JSON-API's (`POST`),
met echte gegenereerde afbeeldingen voor de foto-upload, precies zoals
`tests/test_authenticity.py` dat al deed. Test 1 doorloopt: registreren
(verkoper/koper/admin) → advertentie aanmaken + alle 12 foto's uploaden →
indienen (laag risico → automatisch live) → browsen/detailpagina anoniem →
afrekenen → betaling simuleren → verzenden → admin markeert bezorgd →
Stripe Connect-simulatie → ontvangst bevestigen (betaling vrijgegeven) →
een tweede bestelling met een counterfeit-melding → berichten, admin-notities,
geschil afhandelen → alle adminpagina's. Test 2 forceert bewust een
hoog-risicoscore (ontbrekende foto + niet-kloppende batchcode + nieuw account
+ hoge prijs), bevestigt dat de advertentie in de reviewwachtrij belandt,
en doorloopt `request_more_evidence` → opnieuw indienen via de website.

Alle 166 eerder bestaande tests (`test_auth.py`, `test_buyer_protection.py`,
`test_authenticity.py`) zijn na deze wijzigingen opnieuw gedraaid en slagen
nog steeds stuk voor stuk — de website-laag heeft geen bestaande
backend-code gewijzigd op een manier die dat gedrag raakt, op twee kleine,
bewuste verruimingen na: `GET /listings/<id>` en de bijbehorende foto-route
zijn niet langer inlog-verplicht (nodig voor een publiek browsbare
marktplaats — batchcode/barcode/aankoopgegevens blijven wel alleen zichtbaar
voor eigenaar/admin), en er is een nieuwe publieke `GET /listings` bijgekomen
voor de homepage-grid.

### Nog niet gebouwd / bewuste beperkingen

- **Geschilbewijs als foto**: er is nog geen losse upload-voorziening voor
  geschillen (`POST /disputes/<id>/evidence` verwacht een `file_ref` die de
  cliënt al zou moeten hebben) — de website ondersteunt daarom alleen
  schriftelijke toelichtingen bij een geschil, niet het uploaden van foto's
  daarbinnen. Advertentie-bewijsfoto's (de 12 categorieën) werken wel
  volledig.
- **Zoeken/filteren** op de homepage is bewust eenvoudig (merk/naam-substring
  + staat) — geen facetten, sortering, of paginering.
- **E-mail/push-notificaties**: `notifications` blijft een interne tabel;
  er is geen bezorgkanaal.
- Alles onder "Nog niet gebouwd" bij Buyer Protection en Authenticity
  hierboven geldt onverkort ook voor de website — deze laag voegt geen
  nieuwe achterliggende functionaliteit toe, alleen een gebruikersinterface
  bovenop wat er al was.
