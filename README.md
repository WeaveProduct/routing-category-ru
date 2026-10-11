# routing-category-ru

Списки для раздельного туннелирования WeaveVPN: российские IP-адреса и сайты по категориям —
банки, госсервисы, магазины и т. д. Из них собираются rule-set'ы sing-box, которые приложение
скачивает и обновляет само.

## Категории

| id | В приложении | Из чего |
|---|---|---|
| `ru-banks` | Банки и платежи | `category-bank-ru` + приложения банков на Android |
| `ru-gov` | Госуслуги и госсервисы | `category-gov-ru` + Госуслуги, Налоги ФЛ, Мой налог |
| `ru-shops` | Маркетплейсы и магазины | `category-ecommerce-ru`, `category-retail-ru` |
| `ru-video` | Кино и видео | `category-entertainment-ru` |
| `ru-yandex` | Яндекс | `yandex` |
| `ru-vk` | VK, Одноклассники, Mail.ru | `mailru-group` |
| `ru-telecom` | Операторы связи | `category-telecom-ru` |
| `ru-travel` | Транспорт и путешествия | `category-travel-ru` |
| `ru-sites` | Все российские сайты | `category-ru`: зоны .ru, .su, .рф и всё перечисленное выше |
| `ru-ip` | Российские IP-адреса | `ru` — по данным RIPE NCC |

Порядок в `categories.json` важен: если сайт попал в две категории, срабатывает та, что выше
(например, `mtsbank.ru` есть и в банках, и у операторов — сработают банки). Поэтому общие
категории стоят в конце. Сборка печатает все такие пересечения.

Нужны оба вида списков: российский сайт за зарубежным CDN виден только по домену, а программу,
которая ходит по IP без имени, ловит только список адресов.

## Как устроено

```
categories.json      категории для приложения
domains/             списки сайтов, имя файла = имя списка
ip/ru-extra.txt      добавки к российским адресам
tests/match.txt      что обязано попадать в категорию, а что — нет
scripts/build.py     сборка
.github/workflows/   сборка при каждом push и раз в сутки, публикация
```

## Как добавить сайт

Найдите подходящий список в `domains/` и допишите строку (синтаксис
[v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)):

| Строка | Что ловит |
|---|---|
| `sberbank.ru` | домен и все поддомены (`online.sberbank.ru`), но не `notsberbank.ru` |
| `full:online.sberbank.ru` | только этот домен |
| `include:sberbank` | весь список `sberbank` |
| `keyword:…`, `regexp:…` | подстроку и регулярное выражение — медленно и ловит лишнее, лучше не надо |

`#` — комментарий. Кириллические домены — в punycode: `яндекс.рф` → `xn--d1acpjx3f.xn--p1ai`
(сборка подскажет, если забыть). Метки вроде `@ads` на сборку не влияют.

Новый список — файл в `domains/` без расширения: латиница в нижнем регистре, цифры, дефис.
Новая категория — запись в `categories.json`:

| Поле | |
|---|---|
| `id` | латиница, цифры, дефис. После публикации не менять: на него ссылаются правила в приложении |
| `title`, `description` | название и описание в приложении |
| `geosite` | списки сайтов из `domains/` |
| `geoip` | списки адресов: `ru` или свои из `ip/` |
| `android` | пакеты приложений категории: правило «без VPN» исключит их из VPN целиком |

Хотя бы один из `geosite` / `geoip` обязателен. После правок добавьте пару строк в `tests/match.txt`.

## IP-адреса

`geoip-ru` сборка каждый раз берёт из статистики RIPE NCC (`delegated-ripencc-extended-latest`):
всё, что зарегистрировано в России, — около 11 тысяч сетей IPv4 и IPv6, ~50 КБ. Руками этот список
не ведём; если чего-то не хватает — в `ip/ru-extra.txt`. Свой отдельный список адресов — файл
`ip/<имя>.txt`, из него получится `geoip-<имя>`.

## Сборка у себя

Нужны Python 3.10+ и sing-box (подойдёт и тот, что лежит в WeaveVPN):

```bash
python scripts/build.py --sing-box путь/к/sing-box
```

Результат — в `build/`. Сборка останавливается, если в списке ошибка, RIPE отдал подозрительно
мало адресов или не сошлась проверка из `tests/match.txt`.

## Что публикуется

GitHub Actions собирает при каждом push в `main` и раз в сутки. Если что-то изменилось, ветка
`release` перезаписывается одним коммитом с новой сборкой, а `index.json` и `.srs` выходят
GitHub Release'ом с датой в названии. Pull request только проверяется. Если в публичном
репозитории 60 дней нет активности, GitHub может выключить ежедневный запуск — тогда его
включают снова во вкладке Actions.

```
index.json            манифест для приложения
geoip-ru.srs          rule-set'ы sing-box (формат версии 2 — читает sing-box 1.10 и новее)
geosite-<список>.srs
json/<имя>.json       их исходники        — только в ветке release
text/<имя>.txt        те же списки текстом — только в ветке release
```

Одни и те же файлы:

- `https://github.com/WeaveProduct/routing-category-ru/releases/latest/download/index.json`
- `https://cdn.jsdelivr.net/gh/WeaveProduct/routing-category-ru@release/index.json`
- `https://raw.githubusercontent.com/WeaveProduct/routing-category-ru/release/index.json`

### index.json

```json
{
  "schema": 1,
  "version": "20261011004133",
  "updated": "2026-10-11T00:41:33Z",
  "sources": { "ripe": "20261010" },
  "categories": [
    {
      "id": "ru-banks",
      "title": "Банки и платежи",
      "description": "Сбер, Т-Банк, ВТБ, Альфа-Банк, Газпромбанк, СБП, Mir Pay и другие",
      "rule_sets": ["geosite-category-bank-ru"],
      "android": ["ru.sberbankmobile", "com.idamob.tinkoff.android"]
    }
  ],
  "rule_sets": {
    "geosite-category-bank-ru": {
      "type": "domain",
      "path": "geosite-category-bank-ru.srs",
      "sha256": "f3d04b70…",
      "size": 2428,
      "rules": 351
    }
  }
}
```

Приложению достаточно скачать `index.json`, показать `categories` и скачать `.srs` включённых
категорий; файл с тем же `sha256` перекачивать не нужно. Порядок `categories` — тот же, что в
`categories.json`, по нему и решаются пересечения. `schema` изменится, только если формат
станет несовместимым.

Файлы для Xray (`.dat`) не собираются: в WeaveVPN правила выполняет sing-box.

## Источники и лицензии

- Списки сайтов в `domains/`, кроме `category-telecom-ru`, взяты из
  [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) на коммите
  [`c6adacf`](https://github.com/v2fly/domain-list-community/tree/c6adacf29cc4fb05e04651fcf03e00989ef9f797/data)
  (9 октября 2026), лицензия MIT — [licenses/v2fly-domain-list-community.txt](licenses/v2fly-domain-list-community.txt).
  Дальше они ведутся здесь; что изменилось у v2fly, можно сравнить с этим коммитом.
- Российские адреса — [статистика RIPE NCC](https://ftp.ripe.net/pub/stats/ripencc/).
