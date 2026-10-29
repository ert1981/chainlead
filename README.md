# ChainLead Agent

موتور قابل نصب برای کشف پروژه‌های Web3، تشخیص فرصت‌های همکاری و امتیازدهی لیدها **بر پایه شواهد قابل حسابرسی** — طراحی‌شده برای اینکه ایجنت‌های دیگر از طریق **MCP**، **REST API** یا **Python SDK** از آن استفاده کنند.

- هسته فقط با کتابخانه استاندارد پایتون (۳.۱۰+) اجرا می‌شود؛ هیچ وابستگی اجباری ندارد.
- کاملاً **فقط‌خواندنی** است: کلید خصوصی، Seed Phrase یا امضای تراکنش را نمی‌پذیرد و هیچ متد نوشتنی RPC ندارد.
- هر ادعا در گزارش فرصت، منبع (URL) و زمان مشاهده دارد.
- بودجه را موتور کنترل می‌کند، نه مدل زبانی.

## شروع سریع (۳۰ ثانیه، بدون اینترنت)

```bash
pip install -e .
chainlead demo --out demo-briefs          # روی اکوسیستم «ساختگی» داخلی اجرا می‌شود
```

خروجی: جدول رتبه‌بندی + `demo-briefs/report.json` + یک Opportunity Brief (Markdown و JSON) برای هر لید واجد شرایط یا نیازمند بررسی.

> داده نمایشی کاملاً ساختگی است (دامنه‌های `.example`). موارد سخت عمداً در آن گذاشته شده‌اند: پروژه تکراری، هویت مشکوک که قرارداد پروژه دیگری را ادعا می‌کند، پروژه دارای Prompt Injection و انتشار اسپم، رویداد Reorg‌شده، پروژه قدیمی، پروژه خارج از بازار هدف، و پروژه بدون راه تماس تأییدشده.

## اجرای واقعی

```bash
cp .env.example .env     # کلیدهای خودت را وارد کن (BYOK)
export GITHUB_TOKEN=...                       # اختیاری؛ سقف GitHub را از 60 به 5000 درخواست در ساعت می‌برد
export CHAINLEAD_RPC_8453=https://<your-base-rpc>   # برای بررسی آن‌چین روی Base
export EXPLORER_API_KEY=...                   # اختیاری؛ تأیید سورس قرارداد (Etherscan V2)
chainlead run examples/campaign_base_bridge.json --out briefs --lang fa
```

نمونه کمپین (`examples/campaign_base_bridge.json`):

```json
{
  "name": "base-bridge-q4",
  "offer": "bridge_dev",
  "target_sectors": ["defi", "infrastructure", "bridge"],
  "target_chains": [8453],
  "allowed_sources": ["github", "public_web", "rpc", "explorer"],
  "github_queries": ["topic:cross-chain solidity", "base bridge in:readme"],
  "max_candidates": 20, "max_leads": 10, "budget_usd": 0.50, "freshness_days": 90
}
```

پیشنهادهای آماده (`chainlead offers`): `smart_contract_dev`, `bridge_dev`, `security_audit`, `onchain_analytics` — یا یک پیشنهاد سفارشی با کلیدواژه‌ها و وزن ارتباط هر نوع سیگنال.

## اتصال ایجنت‌ها

### MCP (stdio)

```json
{
  "mcpServers": {
    "chainlead": {
      "command": "chainlead",
      "args": ["mcp"],
      "env": { "CHAINLEAD_DB": "/path/chainlead.db", "GITHUB_TOKEN": "...", "CHAINLEAD_AGENT_MAX_BUDGET_USD": "1.0" }
    }
  }
}
```

ابزارها: `chainlead_list_offers`، `chainlead_create_campaign`، `chainlead_run_campaign`، `chainlead_list_leads`، `chainlead_get_brief`، `chainlead_get_budget`.
عمداً ابزاری برای افزایش بودجه وجود ندارد و بودجه کمپین‌هایی که ایجنت می‌سازد به `CHAINLEAD_AGENT_MAX_BUDGET_USD` محدود است. تغییر بودجه فقط از CLI یا REST با کلید ممکن است.

### REST API

```bash
CHAINLEAD_API_KEYS="acme:secret-key-1" chainlead serve --host 0.0.0.0 --port 8787
```

| متد | مسیر | کاربرد |
|---|---|---|
| GET | `/health` | وضعیت و ارائه‌دهندگان فعال |
| GET | `/offers` | فهرست پیشنهادها |
| POST | `/campaigns` | ساخت کمپین |
| POST | `/campaigns/{id}/run` (`?async=true`) | اجرا (همزمان یا پس‌زمینه) |
| GET | `/jobs/{job_id}` | وضعیت اجرای پس‌زمینه |
| GET | `/campaigns/{id}/leads?status=qualified` | لیدها |
| GET | `/leads/{lead_id}/brief?format=markdown&lang=fa` | گزارش فرصت |
| GET/PUT | `/campaigns/{id}/budget` | بودجه و هزینه |
| GET | `/campaigns/{id}/calls` | دفتر هزینه هر فراخوانی |
| GET | `/projects`, `/projects/{key}` | پروژه‌ها، شواهد و رویدادهای آن‌چین |

احراز هویت: `Authorization: Bearer <key>`. هر کلید یک tenant است و داده‌ها کاملاً جدا هستند. بدون کلید، سرور فقط روی `127.0.0.1` اجرا می‌شود.

### Python SDK

```python
from chainlead.sdk import ChainLeadClient, LocalChainLead

cl = ChainLeadClient("http://127.0.0.1:8787", api_key="secret-key-1")   # یا LocalChainLead("x.db")
c = cl.create_campaign(name="audit-leads", offer="security_audit", allowed_sources=["github", "public_web"],
                       github_queries=["topic:defi base"], target_chains=[8453], budget_usd=0.2)
report = cl.run_campaign(c["id"])
for lead in cl.leads(c["id"], "qualified"):
    print(cl.brief(lead["lead_id"], "markdown", "fa"))
```

## خط لوله

1. **پیشنهاد تجاری** — بدون آن «لید خوب» معنا ندارد.
2. **کشف** — جست‌وجوی GitHub + پروژه‌های بذر کمپین؛ حذف Fork و Archive؛ ادغام تکراری‌ها بر اساس سازمان GitHub یا دامنه.
3. **شواهد** — GitHub (نسخه‌ها، فعالیت ۹۰ روزه، README، آدرس قراردادها)، وب‌سایت رسمی (با رعایت robots.txt: شبکه‌ها، استخدام، ممیزی، تأمین مالی، یکپارچه‌سازی، تیم، کانال‌های تماس).
4. **هویت** — فقط پیوندهای دوطرفه سایت↔GitHub و آدرس‌هایی که منابع رسمی منتشر کرده‌اند. کلید قرارداد `chain_id + address` است؛ اگر دو پروژه یک قرارداد را ادعا کنند، ضعیف‌تر جریمه می‌شود.
5. **آن‌چین** — بررسی بایت‌کد و رویدادهای `Upgraded` / `AdminChanged` / `Initialized` / `OwnershipTransferred`، از جدید به قدیم و با سقف درخواست. کلید رویداد `chain_id + tx_hash + log_index`، همراه شماره و هش بلاک؛ وضعیت `pending → safe → finalized` و علامت‌گذاری `orphaned` پس از Reorg.
6. **سیگنال و امتیاز** — جدا کردن مشاهده / استنباط / فرصت، سپس:
   - `L = 0.30F + 0.25B + 0.20C + 0.15D + 0.10R`
   - `O = 0.35N + 0.25T + 0.20E + 0.20Q`
7. **تکمیل با بودجه** — فراخوانی‌های پولی (تأیید قرارداد، تکمیل تماس) فقط برای بهترین گزینه‌ها و قبل از اجرا از بودجه رزرو می‌شوند.
8. **رتبه‌بندی و گزارش** — Opportunity Brief به فارسی یا انگلیسی.

### قانون تصمیم

امتیاز بالا کافی نیست. لید فقط وقتی `qualified` است که علاوه بر حد امتیازها: هویت مطمئن باشد، محتوای مشکوک نداشته باشد، اطمینان داده کافی باشد، حداقل یک سیگنال رسمی یا تأییدشده و یک راه تماس رسمی داشته باشد. وگرنه `review` (بررسی انسانی) یا `rejected`.

## امنیت

- رد خودکار هر ورودی شامل کلید خصوصی، Seed Phrase یا فیلدهایی با این نام‌ها (در CLI، REST، MCP و SDK).
- RPC فقط متدهای خواندنی را مجاز می‌داند؛ GraphQL فقط query.
- متن‌های وب، README و نسخه‌ها «داده» هستند: پاک‌سازی کاراکترهای پنهان، محدودیت طول، تشخیص Prompt Injection. پروژه پرچم‌خورده هرگز خودکار واجد شرایط نمی‌شود و هزینه پولی برایش خرج نمی‌شود.
- مقاومت در برابر دست‌کاری امتیاز: سقف ۳ سیگنال از هر نوع، تشخیص انتشار انبوه (signal flood)، کیفیت پایین برای ادعاهای تأییدنشده شخص ثالث.
- جداسازی کامل tenantها در همه جداول؛ مقایسه کلید API در زمان ثابت؛ سقف اندازه درخواست.
- محافظ SSRF برای وب‌سایت‌های نامطمئن: فقط IP عمومی، اتصال به همان IP بررسی‌شده (ضد DNS rebinding)، بررسی دوباره هر redirect، فقط پورت 80/443، سقف حجم و زمان.
- محدودیت نرخ REST برای هر tenant/IP و timeout سوکت؛ بودجه فقط عدد متناهی بین ۰ و ۱٬۰۰۰٬۰۰۰ دلار.
- جزئیات: [SECURITY_REVIEW.md](SECURITY_REVIEW.md)

## هزینه

- هر فراخوانی در جدول `provider_calls` با هزینه، وضعیت و کش ثبت می‌شود؛ گزارش هر اجرا «هزینه هر لید واجد شرایط» را می‌دهد.
- درخواست‌های تکراری از کش سرو می‌شوند (اجرای مجدد کمپین هزینه پولی دوباره ندارد).
- منابع رایگان همیشه قبل از منابع پولی برای همان قابلیت انتخاب می‌شوند.
- خطای ارائه‌دهنده = بازگشت هزینه رزروشده.

## تست

```bash
pip install -e ".[dev]"
pytest -q                        # ۲۱۴ تست: unit, integration, security, e2e, tool
CHAINLEAD_LIVE=1 pytest -m live  # تست دود روی GitHub واقعی (نیاز به اینترنت)
```

ارائه‌دهندگان واقعی (GitHub، وب‌سایت، JSON-RPC، Explorer، The Graph) در تست‌ها با سرورهای HTTP محلی که پاسخ‌های واقعی را شبیه‌سازی می‌کنند آزموده می‌شوند.

## ابزار پولی برای ایجنت‌ها (OpenSea Tools / ERC-8257 / x402)

`chainlead tool-serve` یک endpoint پولی و هم‌زمان (synchronous) می‌سازد که ایجنت‌های دیگر با USDC روی Base برایش پول می‌دهند:

| مسیر | توضیح |
|---|---|
| `POST /v1/leads/discover` | کشف و امتیازدهی لید (پولی، x402 v1 + v2، متادیتای Bazaar) |
| `GET/HEAD /v1/leads/discover` یا POST خالی | پاسخ فوری 402 با شرایط پرداخت |
| `/.well-known/ai-tool/chainlead-web3-lead-discovery.json` | مانیفست ERC-8257 |
| `/static/icon.png` | آیکون |
| `/register-onchain` | صفحه ثبت در ToolRegistry روی Base با امضای خود سازنده (با بررسی تکراری) |
| `/health` | وضعیت |

- ترتیب پرداخت: اعتبارسنجی ورودی (422) ← verify ← رزرو replay ← اجرا ← settle. نتیجه فقط بعد از settle موفق برگردانده می‌شود. اگر هیچ کاندیدی پیدا نشود هزینه‌ای گرفته نمی‌شود.
- سقف‌ها: حداکثر ۸ کاندید، ۵ لید، ۳ جستجوی GitHub، بودجه زمانی ۳۵ ثانیه، ۲ اجرای هم‌زمان (بیشتر = 503 قبل از پرداخت)، ۶۰ درخواست در دقیقه برای هر IP.
- فقط منابع رایگان (GitHub، وب‌سایت رسمی، RPC عمومی Base)؛ هزینه داده صفر. داده کمپین بعد از هر فراخوانی پاک می‌شود.
- قیمت پیشنهادی: `0.50` USDC برای هر فراخوانی (`CHAINLEAD_PRICE_USDC`). تغییر قیمت یا URL، hash مانیفست را عوض می‌کند.

متغیرها (همه عمومی‌اند؛ برای دریافت پول هیچ کلید خصوصی لازم نیست):

```
PUBLIC_BASE_URL=https://<service>.onrender.com   # الزامی برای مانیفست
X402_MODE=facilitator                           # PayAI؛ off = غیرفعال (503)
X402_PAY_TO=0x...                               # گیرنده USDC
TOOL_CREATOR_ADDRESS=0x...                      # امضاکننده ثبت
CHAINLEAD_PRICE_USDC=0.50
GITHUB_TOKEN=...                                            # توکن بدون scope؛ عملاً لازم (بدون آن ۶۰ درخواست در ساعت)
```

استقرار روی Render (رایگان): `render.yaml` و `Dockerfile` آماده‌اند. سرویس رایگان بعد از بیکاری می‌خوابد؛ قبل از `tool-sdk verify` یا ثبت، یک بار `/health` را صدا بزن تا بیدار شود.

## Docker

```bash
docker build -t chainlead . && docker run -p 8000:8000 --env-file .env chainlead   # ابزار پولی x402
docker compose up --build        # REST API روی :8787 ، داده در volume
```

## محدودیت‌های نسخه اول

- یک شبکه EVM هدف (پیش‌فرض Base) پیشنهاد می‌شود؛ شبکه‌های دیگر در registry هستند اما هر کدام RPC جداگانه می‌خواهند.
- اسکن رویدادها روی RPCهای عمومی با سقف درخواست محدود است؛ برای پوشش کامل بازه تازگی، RPC با سقف بالاتر یا `log_range_override` لازم است.
- تکمیل تماس پولی (مثل پایگاه‌های داده B2B) فقط به‌صورت رابط وجود دارد؛ در نسخه اول فقط کانال‌های عمومی و رسمی پروژه استخراج می‌شوند.
- The Graph به‌صورت اتصال‌دهنده عمومی است و به شناسه Subgraph مخصوص هر پروژه نیاز دارد (`graph_queries` در پروژه بذر).
- وزن‌ها نقطه شروع‌اند و باید با نتایج واقعی فروش تنظیم شوند.

> نشانه‌ها فقط نیاز «احتمالی» را نشان می‌دهند؛ اثبات بودجه یا قصد خرید نیستند. پیش از تماس، منابع را بررسی کن و قوانین ضداسپم و حریم خصوصی را رعایت کن.

مجوز: MIT
