# SecondScent — Architectuur, database, payments & implementatieplan

Status: ontwerpdocument (fase A–F), vóór implementatie.
Scope: dit document behandelt het **systeem achter** SecondScent — accounts, advertenties, bestellingen, betalingen, kopersbescherming, notificaties, beveiliging, database en adminomgeving. De bestaande publieke site (`secondscent.html`) wordt **niet aangepast** en blijft zoals hij is; dit ontwerp bouwt daar los overheen.

---

## A. Analyse van de huidige codebase

`secondscent.html` is op dit moment **één self-contained statisch HTML-bestand** (~1,9 MB, incl. ingebedde foto's als base64): inline CSS, inline JS, een eigen i18n-systeem (`data-i18n` + een `I18N`-object voor 5 talen), en vier voorbeeldadvertenties met hardcoded data. Er is:

- **geen backend** — geen server, geen database, geen API;
- **geen accounts** — "Inloggen" en "Nu verkopen" zijn knoppen zonder functionaliteit;
- **geen echte data** — de 4 advertenties staan letterlijk in de HTML;
- **geen betaalflow** — de "koop"-knoppen bestaan niet echt.

Wat wél sterk is en behouden moet blijven als **design system**: de huisstijl (donkergroen `#1E3A2F` + crème `#F3EEE1` + oranje accent `#DE6B2C`), het lettertype-paar Fraunces (kop) + Work Sans (body), de kaartlayout voor advertenties, en het i18n-patroon.

**Belangrijke architecturale conclusie:** een marketplace met accounts, rollen, een orderstatusmachine, betalingen, dashboards en een adminomgeving kan niet fatsoenlijk **binnen hetzelfde ene HTML-bestand** gebouwd worden — dat zou een onderhoudbaar, veilig systeem in de weg staan (geen server-side authorisatie mogelijk in een statisch bestand, geen geheimen te bewaren, geen database). Daarom:

- `secondscent.html` blijft ongewijzigd staan als de publieke, taal-ondersteunende marketing/browse-pagina.
- Er komt een **nieuwe, aparte applicatie** (`secondscent-platform/`) die het echte systeem bevat: accounts, advertenties-CRUD, checkout, dashboards, admin. Deze hergebruikt de kleuren/typografie/componentstijl 1-op-1, zodat het oogt als dezelfde site.
- Op termijn kan de statische pagina's advertentie-grid vervangen worden door live data uit deze backend (server-rendered of via een API) — dat is een latere, aparte stap en niet nodig om nu al te beginnen.

---

## B. Voorgestelde architectuur

```
                     ┌─────────────────────────┐
                     │   secondscent.html       │  (ongewijzigd, publiek)
                     └─────────────────────────┘

┌───────────────────────────── secondscent-platform ─────────────────────────────┐
│                                                                                  │
│  Web/App laag (Flask, modulair via blueprints)                                  │
│   ├─ auth        (registratie, login, sessies, wachtwoordreset)                 │
│   ├─ listings     (advertenties CRUD, foto-upload, moderatie)                   │
│   ├─ orders       (checkout, statusmachine, verzending, inspectieperiode)       │
│   ├─ payments     (Stripe Connect: onboarding, PaymentIntents, transfers)       │
│   ├─ disputes     (probleemmeldingen, bewijs, admin-beslissing)                 │
│   ├─ notifications(interne meldingen + e-mail)                                  │
│   └─ admin        (afgeschermde beheeromgeving, aparte auth-scope)              │
│                                                                                  │
│  Achtergrondwerker (cron/worker)                                                │
│   ├─ sluit inspectieperiodes af → triggert payout-transfer                      │
│   ├─ verzendherinneringen / deadline-bewaking                                   │
│   └─ webhook-retry & opruiming                                                  │
│                                                                                  │
│  Data laag: PostgreSQL (schema § C)                                             │
│  Bestandsopslag: S3-compatible object storage voor foto's/bewijs                │
│  Externe dienst: Stripe Connect (betalingen, KYC, payouts) — zie § D            │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Waarom deze keuzes:**

- **Flask**, modulair opgezet met *blueprints* (één per domein hierboven) — dezelfde stack die al werkt in deze omgeving (bewezen met het eerder gebouwde beheerpaneel), goed te testen, en eenvoudig genoeg dat je niet vastloopt in framework-magie. **FastAPI is een geldig alternatief** (async, ingebouwde validatie via Pydantic) als je zelf verder bouwt in een omgeving met volledige packagetoegang — het onderliggende ontwerp (modules, database, statusmachine, Stripe-flow) verandert daar niet door.
- **PostgreSQL**, niet SQLite: je vraagt zelf om constraints, indexes en relaties die serieus gehandhaafd worden — dat wil je in een echte RDBMS, met transacties en (waar mogelijk) triggers die *op databaseniveau* voorkomen dat een bug een ongeldige statusovergang wegschrijft.
- **Server-side rendering + JSON-endpoints voor dynamische delen** (checkout, dashboards) — geen aparte SPA-framework-keuze nodig voor de MVP; dat is een latere optimalisatie, geen blokkerende beslissing nu.
- **Achtergrondwerker als apart proces** (niet in de webrequest zelf): het vrijgeven van een payout na de inspectieperiode moet gebeuren ook als niemand op dat moment op de site zit.

---

## C. Databasewijzigingen — schema

Volledig schema, met constraints/indexes, staat in [`db/schema.sql`](../db/schema.sql) (uitvoerbaar, en al lokaal geverifieerd tegen een echte PostgreSQL 16-instantie — zie § F, Fase 0). Samenvatting van de belangrijkste tabellen en *waarom*:

| Tabel | Doel |
|---|---|
| `users` | Eén account, kan koper én verkoper zijn (`is_buyer`/`is_seller` vlaggen i.p.v. een rigide rol-enum) |
| `user_verifications` | **Expliciet gescheiden** van `users` — legt per verificatietype (e-mail, telefoon, ID, Stripe-KYC) vast: status + wanneer + door wie geverifieerd. Dit is de tabel die het onderscheid *geverifieerd vs. alleen opgegeven* afdwingt: een frontend mag een claim **nooit** als geverifieerd tonen tenzij hier een `verified`-rij bestaat. |
| `stripe_connect_accounts` | 1-op-1 met verkopers; spiegelt Stripe's `charges_enabled`/`payouts_enabled`/onboarding-status |
| `listings` | Alle velden uit je spec (merk, naam, variant, ml, batchcode, aankoopbron, conditie, doos, aankoopbewijs, …) |
| `listing_photos` | Los van `listings` (1-op-veel), met volgorde |
| `orders` | De kern van de statusmachine (§ D/E); bevat **nooit** een door de client aangeleverde prijs — alle bedragen worden server-side herberekend bij checkout |
| `order_status_history` | Append-only audit trail van elke statusovergang: wie, wanneer, waarom |
| `shipments` | Trackinggegevens, gekoppeld aan een order |
| `disputes` | Probleemmeldingen, bewijs (als JSON-referenties naar opslag), admin-uitspraak |
| `refunds` / `transfer_reversals` | Weerspiegelen de Stripe-objecten 1-op-1 (zie § D — dit zijn *twee gescheiden* acties in Stripe, dus ook hier) |
| `payouts` | Spiegelt Stripe `Payout`-objecten voor het verkopersdashboard |
| `webhook_events` | Elke binnenkomende Stripe-webhook wordt hier **eerst** met een unieke `stripe_event_id` weggeschreven — dit is je idempotentie-laag tegen dubbele verwerking |
| `notifications` | Eén tabel, getypeerd via `type` + `payload jsonb` |
| `audit_logs` | Generieke, append-only log voor gevoelige acties (admin-acties, statuswijzigingen, verificatie-wijzigingen) |
| `risk_flags` | Losstaand van moderatie-acties zelf, zodat risicoscore en daadwerkelijke actie apart te auditen zijn |
| `reviews` | **Uniek per `order_id`** — een review kan alleen bestaan bij een `completed` order. Dit is de databaseregel die neppe reviews structureel onmogelijk maakt: er is geen pad om een review te plaatsen zonder een echte, afgeronde transactie. |

**Statusmachine op databaseniveau:** naast applicatielogica staat er een `order_status_transitions`-tabel die *toegestane* overgangen expliciet opsomt, plus een Postgres-trigger (`enforce_order_status_transition`) die **elke** `UPDATE` op `orders.status` daartegen valideert en weigert als de overgang niet in de tabel staat — zie § F Fase 0 voor het testbewijs dat een ongeldige overgang (bijv. `paid → completed`, de inspectieperiode overslaand) daadwerkelijk wordt geweigerd door de database zelf, niet alleen door applicatiecode.

---

## D. Payment-architectuur (Stripe Connect)

**Bron:** actuele officiële Stripe-documentatie (docs.stripe.com), geraadpleegd bij het schrijven van dit document. Belangrijkste bronnen onderaan deze paragraaf.

### Gekozen model: Express-accounts + "Separate charges and transfers"

- **Accounttype: Stripe Connect *Express*.** Dit is Stripe's eigen aanbevolen type voor precies dit scenario — individuele particuliere verkopers, Stripe host de onboarding en identiteitsverificatie (KYC), het platform bepaalt het laadtype en uitbetalingsschema. Stripe noemt zelf consumer-marktplaatsen (verhuur, ritten) als schoolvoorbeeld. *Let op:* Stripe is bezig met een nieuwe "Accounts v2"-API; de combinatie "Express Dashboard + Stripe draagt het risico van negatieve saldi" zit daar nog in **public preview**. Voor een productiesysteem nu: bouw op de **volwassen, volledig algemeen beschikbare Express-flow** (Accounts v1); heroverweeg v2 zodra die combinatie GA is.
- **Betaalpatroon: "Separate charges and transfers"** (niet "Destination charges"). Dit is exact het patroon dat Stripe zelf beschrijft voor "geld vasthouden tot een dienst/levering bevestigd is": de koper betaalt op de rekening van *het platform* (niet direct naar de verkoper); pas na de inspectieperiode maakt het platform een aparte **Transfer** naar de verkoper.

### De flow, stap voor stap

1. **Checkout** → platform maakt een `PaymentIntent` aan **op de eigen (platform-)rekening**, met een `transfer_group` gelijk aan de order-id. Bedrag wordt **altijd server-side herberekend** uit `listings.asking_price_cents` + verzendkosten + kopersbeschermingsfee + eventuele belasting — nooit uit een door de client meegestuurd bedrag.
2. Koper rondt betaling af. Geld staat op het **platformsaldo**, niet bij de verkoper. Order → `paid`.
3. Verkoper verzendt, tracking wordt gekoppeld. Order → `shipped` → (bij bevestigde bezorging, handmatig of via trackingstatus) → `delivered`.
4. **Inspectieperiode** start (bijv. 48–72 uur, instelbaar). Order → `inspection_period`.
5. **Geen probleem gemeld binnen de termijn** → achtergrondwerker maakt een `Transfer` aan naar de verkoper's Stripe-account, gekoppeld via `source_transaction` (zodat de transfer altijd slaagt en automatisch de eigen wacht-op-beschikbaarheid van de oorspronkelijke charge overneemt — geen race conditions met het platformsaldo). Order → `completed`.
6. **Wél een probleem gemeld** → order → `issue_reported` → `under_review`; de payout-transfer wordt simpelweg **niet** aangemaakt (zie hieronder — dit is de eigen, betrouwbare vorm van "pauzeren").
7. **Uitkomst na review:**
   - Terecht probleem, vóór transfer → gewoon een `Refund` op de PaymentIntent (er is nog niets naar de verkoper overgemaakt, dus niets terug te draaien).
   - Terecht probleem, ná transfer (zou niet moeten voorkomen bij correcte implementatie van punt 6, maar als vangnet) → `Refund` op de charge **plus apart** een `Transfer Reversal` op de eerder gemaakte transfer — dit zijn in Stripe **twee losse acties**, een refund raakt een eerder gedane transfer niet vanzelf.
   - Ongegrond probleem → order alsnog naar `completed`, transfer alsnog aanmaken.

### Waarom dit "pauzeren van de payout" betrouwbaar is zonder een aparte Stripe-functie

Stripe heeft een feature "pause payments/payouts on connected accounts", maar die is (voor zover in de documentatie na te gaan) op dit moment vooral **Dashboard-gestuurd**, niet duidelijk programmatisch aan te sturen. Dat is geen probleem voor jouw ontwerp: het platform **initieert de payout zelf** (stap 5), dus het enige dat écht nodig is om een payout tegen te houden is: **de transfer simpelweg niet aanmaken totdat de eigen statusmachine dat toestaat.** Dat is volledig in eigen beheer, via de `orders.status`-machine — robuuster dan afhankelijk zijn van een Stripe Dashboard-actie.

Daarnaast: het uitbetalingsschema van elke connected account staat op **`manual`** (Stripe's eigen mechanisme om automatische uitbetaling naar de bankrekening van de verkoper uit te zetten) — dit is de tweede, onafhankelijke laag: zelfs geld dat al *naar* de verkoper's Stripe-saldo is getransferreerd, staat niet automatisch op hun bankrekening totdat het platform expliciet een payout triggert.

### Overige punten uit het onderzoek, verwerkt in het ontwerp

- **Idempotency:** elke muterende Stripe-aanroep (PaymentIntent/Transfer/Refund/Reversal) krijgt een `Idempotency-Key`-header (UUIDv4) — voorkomt dubbele transfers/refunds bij netwerkfouten of retries.
- **Webhooks:** twee scopes nodig — platform-events (`payment_intent.succeeded`, …) én connected-account-events (`account.updated` voor KYC-status, `payout.failed`, …). Handtekeningverificatie via de `Stripe-Signature`-header, met de officiële SDK-helper (nooit handmatig string-vergelijken). Elk event wordt eerst weggeschreven in `webhook_events` (uniek op `stripe_event_id`) vóórdat het verwerkt wordt — dat is de idempotentie-garantie tegen dubbele verwerking bij Stripe's at-least-once bezorging.
- **Fees:** in dit patroon is er geen apart "commissieveld" zoals bij destination charges — de marge is het verschil tussen wat de koper betaalt en wat er getransferd wordt. Moet expliciet in de checkout-berekening (§ order-bedragen) zitten.
- **Belasting/1099-K (VS):** bij dit patroon betaalt het platform de Stripe-verwerkingskosten, wat volgens Stripe's eigen regels betekent dat **het platform** (niet Stripe) verantwoordelijk is voor eventuele 1099-K-rapportage aan Amerikaanse verkopers boven de drempel. Dit is een **fiscaal/juridisch aandachtspunt**, geen technisch detail — laat dit door een boekhouder/jurist bevestigen voordat je in de VS live gaat; voor EU-only lancering is dit niet direct van toepassing maar gelden weer BTW-regels die apart uitgezocht moeten worden.
- **Regiobeperking:** "separate charges and transfers" wordt niet in elk land ondersteund; controleer de actuele lijst van Stripe voordat je een land toevoegt aan je uitbreidingsplan (§ internationale uitbreiding).

**Bronnen (docs.stripe.com, geraadpleegd bij schrijven van dit document):**
`/connect/accounts` · `/connect/express-accounts` · `/connect/charges` · `/connect/separate-charges-and-transfers` · `/connect/account-balances` · `/connect/manage-payout-schedule` · `/connect/pausing-payments-or-payouts-on-connected-accounts` · `/connect/marketplace/tasks/refunds-disputes` · `/connect/disputes` · `/connect/webhooks` · `/webhooks` · `/api/idempotent_requests` · `/connect/tax-reporting`

**Wat dit document bewust NIET doet:** een Stripe-account aanmaken, een API-sleutel genereren, of geld verplaatsen. Dat kan alleen jij doen (Stripe vereist een geverifieerde, aan jou/je bedrijf gekoppelde account) — zie § F Fase 4 voor precies wat daarvoor nodig is.

---

## E. Security- en dreigingsanalyse

| Dreiging | Impact | Mitigatie |
|---|---|---|
| Client stuurt gemanipuleerde prijs/bedrag mee bij checkout | Financieel verlies | Server herberekent **altijd** het volledige bedrag uit `listings` + regels; client-bedrag wordt nooit vertrouwd of zelfs maar gelezen voor de PaymentIntent |
| Onbevoegde toegang tot andermans order/advertentie via directe object-referentie (IDOR) | Datalek, fraude | Elke endpoint controleert server-side of `current_user` daadwerkelijk eigenaar/koper/verkoper van de resource is — nooit alleen op basis van een client-claim |
| Dubbele payout/refund door retry of race condition | Financieel verlies | Idempotency-keys op elke Stripe-mutatie; `webhook_events` met unieke constraint op `stripe_event_id`; DB-transacties rond statuswijziging + Stripe-call |
| Ongeldige orderstatusovergang (bug of misbruik) | Data-integriteit, financieel | DB-trigger die transities tegen een whitelist-tabel valideert (§ C) — hard afgedwongen, niet alleen in applicatiecode |
| Kwaadaardige upload (bijv. een uitvoerbaar bestand vermomd als foto) | Server-compromise, opslag van malware | MIME-sniffing op inhoud (niet alleen extensie), harde grootte-limiet, foto's server-side her-encoderen (verwijdert EXIF + verborgen payloads), opslag buiten de webroot / via object storage met eigen contenttype-afdwinging |
| Brute-force op login | Accountovername | Rate limiting per IP + per account, wachtwoord-hashing met een trage KDF (bijv. scrypt/argon2 via Werkzeug), lockout na herhaalde mislukte pogingen |
| Webhook-spoofing (nep-Stripe-events) | Fraude, valse statuswijzigingen | Verplichte `Stripe-Signature`-verificatie via de officiële SDK, vóór elke verwerking; events zonder geldige handtekening worden geweigerd (4xx), nooit stilzwijgend genegeerd |
| Neppe reviews / neppe verificatie-claims | Vertrouwen van het hele platform | Reviews alleen koppelbaar aan een `completed` order (DB-constraint); UI toont *nooit* een verificatie-claim tenzij er een `verified`-rij in `user_verifications` bestaat — expliciet visueel onderscheiden ("door gebruiker opgegeven" vs. "geverifieerd") |
| Admin-account gecompromitteerd | Volledige platformcontrole | Aparte auth-scope voor admin (geen gedeelde sessie met koper/verkoper-login), elke admin-actie in `audit_logs`, 2FA aanbevolen vóór live-gang |
| Misbruik van het disputeproces (koper claimt ten onrechte "niet ontvangen") | Financieel verlies voor verkopers | Verplichte tracking-koppeling vóór `delivered`; bewijsuitwisseling (foto's, berichten) vastgelegd in `disputes.evidence`; menselijke admin-beslissing vereist, geen automatische refund bij een dispute |
| Enumeratie van gebruikers/advertenties voor scraping of gerichte fraude | Privacy, fraude-voorbereiding | Rate limiting op publieke endpoints, geen voorspelbare sequentiële IDs in publieke URLs (UUID's) |
| Secrets (Stripe-sleutels, DB-wachtwoord) in code of repo | Volledige compromise | Alleen via environment variables / secret manager; nooit gecommit; `.env` in `.gitignore` (zoals ook al bij het beheerpaneel) |

**Algemeen principe dat door het hele systeem loopt:** *server-side authorization en server-side prijsberekening zijn niet optioneel* — elke actie die geld, status, of toegang raakt wordt herverifieerd op de server, ongeacht wat de UI toont of client-side al "gecontroleerd" leek.

---

## F. Gefaseerd implementatieplan

Elke fase heeft een concreet, testbaar eindresultaat. Niet doorgaan naar de volgende fase zonder dat de tests van de huidige fase slagen.

| Fase | Inhoud | Vereist iets van jou? |
|---|---|---|
| **0. Fundament** | Projectstructuur, database-schema + triggers, migratietooling | Nee — nu al gebouwd, zie hieronder |
| **1. Accounts & auth** | Registratie, login, sessiebeveiliging, rollen (koper/verkoper), rate limiting | Nee |
| **2. Advertenties** | CRUD, foto-upload met validatie, alle verplichte velden uit je spec | Nee |
| **3. Orderflow zonder echte betaling** | Volledige statusmachine + alle 13 statussen, met een "test-betaalmodus" (nep-PaymentIntent), zodat de hele bedrijfslogica getest kan worden vóórdat er echt geld bij komt kijken | Nee |
| **4. Stripe Connect (echte betalingen)** | Verkoper-onboarding, checkout, webhooks, transfer-na-inspectie, refunds/reversals | **Ja** — je eigen Stripe-account + testsleutels (§ D). Dit kan ik niet voor je aanmaken. |
| **5. Notificaties** | Interne meldingen voor alle 11 gebeurtenissen uit je spec, + e-mail | Optioneel: een e-mailprovider-account voor echte verzending |
| **6. Koper/verkoper-dashboards** | UI gekoppeld aan echte data, in de bestaande huisstijl | Nee |
| **7. Adminomgeving** | Orders, advertenties, gebruikers, disputes, bewijs, refunds, risk flags, audit trail | Nee |
| **8. Beveiligingsdoorlichting** | Doorloop § E puntsgewijs tegen de dan-werkende implementatie | Nee, wel aanbevolen: een onafhankelijke review |
| **9. Internationalisatie/schaal** | Meertalige backend-teksten (voortbouwend op het bestaande i18n-patroon), regiovalidatie voor Stripe-landen, achtergrondwerker robuustheid | Nee |

**Waarom fase 3 vóór fase 4 (betalingen) staat:** zo kan de volledige statusmachine, autorisatie en dashboards al gebouwd én getest worden zonder dat er een Stripe-account nodig is — pas als dat allemaal aantoonbaar werkt, sluit je er een echte betaalprovider op aan. Dat is ook precies waar in dit traject jouw eigen actie (een Stripe-account aanmaken) nodig wordt; ik kan geen accounts aanmaken of geld verplaatsen namens jou.
