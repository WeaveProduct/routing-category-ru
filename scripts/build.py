#!/usr/bin/env python3
"""Сборка списков маршрутизации в rule-set'ы sing-box.

Берёт сайты из domains/, адреса из ip/ (RU — из статистики RIPE NCC) и категории
из categories.json. Результат кладёт в build/:

  index.json        манифест для приложения: категории, файлы, sha256, размеры
  <имя>.srs         rule-set'ы sing-box: geosite-<список> и geoip-<список>
  json/<имя>.json   их исходники
  text/<имя>.txt    те же списки простым текстом, чтобы смотреть глазами

и прогоняет tests/match.txt через настоящий sing-box. Нужен только Python 3.10+ и sing-box.

    python scripts/build.py                          # RU-адреса качаются из RIPE
    python scripts/build.py --ripe delegated.txt     # или берутся из файла
    python scripts/build.py --sing-box C:/tools/sing-box.exe --out build
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RIPE_URL = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest"
RULE_SET_VERSION = 2  # такие .srs читает sing-box 1.10 и новее
# Меньше этого — RIPE отдал что-то не то: лучше упасть, чем выпустить пустой список.
MIN_RU_NETWORKS = {4: 5000, 6: 500}

LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
DOMAIN_RE = re.compile(rf"{LABEL}(?:\.{LABEL})*")
KEYWORD_RE = re.compile(r"[a-z0-9._-]+")
LIST_NAME_RE = re.compile(r"[a-z0-9!][a-z0-9!-]*")
CATEGORY_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
CATEGORY_KEYS = {"id", "title", "description", "geosite", "geoip", "android"}
# Тип правила в списке → поле правила sing-box и как его писать в text/
FIELDS = {"full": ("domain", "full:"), "domain": ("domain_suffix", ""), "keyword": ("domain_keyword", "keyword:"), "regexp": ("domain_regex", "regexp:")}
PREFIXES = dict(FIELDS.values())

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class BuildError(Exception):
    pass


class Problems:
    """Копит ошибки, чтобы показать все сразу, а не по одной за запуск."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def check(self) -> None:
        if self.errors:
            for error in self.errors:
                print(f"ОШИБКА  {error}", file=sys.stderr)
            raise BuildError(f"ошибок: {len(self.errors)}")


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------- сайты ----------

@dataclass(frozen=True)
class Rule:
    kind: str  # domain — домен с поддоменами, full — только сам домен, keyword, regexp
    value: str


@dataclass
class SiteList:
    name: str
    where: str
    rules: list[Rule] = field(default_factory=list)
    includes: list[tuple[str, str]] = field(default_factory=list)  # (имя списка, где написано)


def parse_rule(kind: str, value: str, where: str, problems: Problems) -> Rule | None:
    if kind not in FIELDS:
        problems.error(where, f"неизвестный тип «{kind}:» — бывают domain:, full:, keyword:, regexp: и include:")
        return None
    if kind == "regexp":
        try:
            re.compile(value)
        except re.error as e:
            problems.error(where, f"плохое регулярное выражение: {e}")
            return None
        return Rule(kind, value)
    if not value.isascii():
        try:
            hint = ": " + value.encode("idna").decode()
        except UnicodeError:
            hint = ""
        problems.error(where, f"«{value}» — кириллицу пишите в punycode{hint}")
        return None
    value = value.lower()
    if not (KEYWORD_RE if kind == "keyword" else DOMAIN_RE).fullmatch(value) or len(value) > 253:
        problems.error(where, f"«{value}» не похоже на домен")
        return None
    return Rule(kind, value)


def load_site_lists(directory: Path, problems: Problems) -> dict[str, SiteList]:
    """Списки в синтаксисе v2fly/domain-list-community: имя файла = имя списка."""
    lists: dict[str, SiteList] = {}
    affiliations: list[tuple[str, Rule, str]] = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        name = path.name
        if not LIST_NAME_RE.fullmatch(name):
            problems.error(rel(path), "имя списка — латиница в нижнем регистре, цифры и дефис, без расширения")
            continue
        if name in lists:
            problems.error(rel(path), f"список «{name}» уже есть: {lists[name].where}")
            continue
        current = lists[name] = SiteList(name, rel(path))
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{rel(path)}:{number}"
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            head, *tail = line.split()
            if head.startswith("include:"):
                if tail:
                    problems.error(where, "фильтры по атрибутам в include: не поддерживаются")
                current.includes.append((head.removeprefix("include:"), where))
                continue
            kind, sep, value = head.partition(":")
            rule = parse_rule(kind, value, where, problems) if sep else parse_rule("domain", head, where, problems)
            if rule is None:
                continue
            current.rules.append(rule)
            for item in tail:
                if item.startswith("&") and len(item) > 1:  # домен заодно входит в другой список
                    affiliations.append((item[1:], rule, where))
                elif not (item.startswith("@") and len(item) > 1):  # атрибуты (@ads и т. п.) на сборку не влияют
                    problems.error(where, f"непонятное «{item}»")
    for target, rule, where in affiliations:
        if target in lists:
            lists[target].rules.append(rule)
        else:
            problems.error(where, f"нет списка «{target}»")
    return lists


def resolve(lists: dict[str, SiteList], problems: Problems) -> dict[str, set[Rule]]:
    """Раскрывает include: — для каждого списка все его правила вместе с вложенными."""
    done: dict[str, set[Rule]] = {}

    def visit(name: str, chain: tuple[str, ...]) -> set[Rule]:
        if name in done:
            return done[name]
        if name in chain:
            problems.error(lists[name].where, "include: по кругу — " + " → ".join(chain + (name,)))
            return set()
        rules = set(lists[name].rules)
        for target, where in lists[name].includes:
            if target in lists:
                rules |= visit(target, chain + (name,))
            else:
                problems.error(where, f"нет списка «{target}»")
        done[name] = rules
        return rules

    for name in lists:
        visit(name, ())
    return done


def domain_key(domain: str) -> list[str]:
    return domain.split(".")[::-1]  # ya.ru рядом с music.ya.ru


def covered(domain: str, suffixes: set[str]) -> bool:
    """Ловит ли домен правило «домен и поддомены» для него самого или для родителя."""
    labels = domain.split(".")
    return any(".".join(labels[i:]) in suffixes for i in range(len(labels)))


def compact(rules: set[Rule]) -> dict[str, list[str]]:
    """Поля правила sing-box без лишнего: то, что уже покрывает родительский домен, выкидываем."""
    suffixes = {r.value for r in rules if r.kind == "domain"}
    kept = {r for r in rules if r.kind != "domain" or not covered(r.value.partition(".")[2], suffixes)}
    kept = {r for r in kept if r.kind != "full" or not covered(r.value, suffixes)}
    result: dict[str, list[str]] = {}
    for kind, (name, _) in FIELDS.items():
        values = sorted({r.value for r in kept if r.kind == kind}, key=domain_key if kind in ("domain", "full") else None)
        if values:
            result[name] = values
    return result


# ---------- адреса ----------

def read_ripe(source: str, country: str) -> tuple[list[Network], str]:
    """Блоки страны из статистики RIPE NCC (delegated-extended) и дата выгрузки."""
    try:
        if source.startswith(("https://", "http://")):
            with urllib.request.urlopen(source, timeout=180) as response:
                text = response.read().decode("utf-8", "replace")
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise BuildError(f"не удалось получить статистику RIPE ({source}): {e}") from e
    networks: list[Network] = []
    date = ""
    for line in text.splitlines():
        f = line.split("|")
        if line.startswith("#") or len(f) < 6:
            continue
        try:
            if f[0].isdigit():  # заголовок: версия|ripencc|серия|записей|с|по|часовой пояс
                date = f[5]
            elif f[1] == country and len(f) >= 7 and f[6] in ("allocated", "assigned"):
                if f[2] == "ipv4":
                    first = ipaddress.IPv4Address(f[3])
                    networks += ipaddress.summarize_address_range(first, first + int(f[4]) - 1)
                elif f[2] == "ipv6":
                    networks.append(ipaddress.IPv6Network(f"{f[3]}/{f[4]}"))
        except ValueError as e:
            raise BuildError(f"RIPE: не разобрана строка «{line}»: {e}") from e
    return networks, date


def read_ip_file(path: Path, problems: Problems) -> list[Network]:
    networks: list[Network] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            networks.append(ipaddress.ip_network(line))
        except ValueError:
            try:
                message = f"«{line}» — адрес не совпадает с началом сети, может, {ipaddress.ip_network(line, strict=False)}?"
            except ValueError:
                message = f"«{line}» — нужен адрес или сеть CIDR, например 203.0.113.0/24"
            problems.error(f"{rel(path)}:{number}", message)
    return networks


def load_ip_lists(directory: Path, ripe: str, problems: Problems) -> tuple[dict[str, list[Network]], str]:
    """ip/<имя>.txt — свой список; ip/<имя>-extra.txt — добавки к автоматическому (ru — из RIPE)."""
    ru, ripe_date = read_ripe(ripe, "RU")
    generated = {"ru": ru}
    lists: dict[str, list[Network]] = {}
    for path in sorted(directory.glob("*.txt")):
        name = path.stem
        base = name.removesuffix("-extra")
        if base != name:
            if base in generated:
                generated[base] += read_ip_file(path, problems)
            else:
                problems.error(rel(path), f"добавки к «{base}», но такого автоматического списка нет (есть: {', '.join(generated)})")
        elif not LIST_NAME_RE.fullmatch(name):
            problems.error(rel(path), "имя списка — латиница в нижнем регистре, цифры и дефис")
        elif name in generated:
            problems.error(rel(path), f"список «{name}» собирается автоматически — добавки кладите в ip/{name}-extra.txt")
        else:
            lists[name] = read_ip_file(path, problems)
    lists.update(generated)
    result: dict[str, list[Network]] = {}
    for name, networks in lists.items():
        result[name] = [n for version in (4, 6) for n in ipaddress.collapse_addresses(x for x in networks if x.version == version)]
        if not result[name]:
            problems.error(f"ip/{name}", "пустой список")
    for version, minimum in MIN_RU_NETWORKS.items():
        count = sum(n.version == version for n in result["ru"])
        if count < minimum:
            problems.error("RIPE", f"для RU всего {count} сетей IPv{version}, ожидалось от {minimum} — RIPE отдал что-то не то")
    return result, ripe_date


# ---------- категории ----------

@dataclass
class Category:
    id: str
    title: str
    description: str
    geosite: list[str]
    geoip: list[str]
    android: list[str]

    @property
    def rule_sets(self) -> list[str]:
        return [f"geosite-{n}" for n in self.geosite] + [f"geoip-{n}" for n in self.geoip]


def strings(raw: dict, key: str, where: str, problems: Problems) -> list[str]:
    value = raw.get(key, [])
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    problems.error(where, f"«{key}» — должен быть списком строк")
    return []


def load_categories(path: Path, sites: dict, ips: dict, problems: Problems) -> list[Category]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BuildError(f"{rel(path)}: неправильный JSON — строка {e.lineno}, столбец {e.colno}: {e.msg}") from e
    if data.get("schema") != 1:
        problems.error(rel(path), "ожидается \"schema\": 1")
    result: list[Category] = []
    for number, raw in enumerate(data.get("categories", []), 1):
        if not isinstance(raw, dict):
            problems.error(rel(path), f"категория №{number} — должна быть объектом {{...}}")
            continue
        where = f"{rel(path)}: {raw.get('id') or f'категория №{number}'}"
        for key in sorted(raw.keys() - CATEGORY_KEYS):
            problems.error(where, f"неизвестное поле «{key}»")
        category = Category(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")).strip(),
            description=str(raw.get("description", "")).strip(),
            geosite=strings(raw, "geosite", where, problems),
            geoip=strings(raw, "geoip", where, problems),
            android=strings(raw, "android", where, problems),
        )
        if not CATEGORY_ID_RE.fullmatch(category.id):
            problems.error(where, "id — латиница в нижнем регистре, цифры и дефис")
        elif any(c.id == category.id for c in result):
            problems.error(where, "такой id уже есть")
        if not category.title:
            problems.error(where, "нет title")
        if not category.geosite and not category.geoip:
            problems.error(where, "нужен хотя бы один список в geosite или geoip")
        for name in category.geosite:
            if name not in sites:
                problems.error(where, f"нет списка сайтов «{name}» в domains/")
        for name in category.geoip:
            if name not in ips:
                problems.error(where, f"нет списка адресов «{name}» в ip/")
        for package in category.android:
            if not PACKAGE_RE.fullmatch(package):
                problems.error(where, f"«{package}» не похоже на имя пакета Android")
        result.append(category)
    if not result:
        problems.error(rel(path), "нет ни одной категории")
    return result


def overlaps(categories: list[Category], resolved: dict[str, set[Rule]]) -> list[str]:
    """Сайты, которые попали сразу в две категории: для них сработает та, что выше в categories.json."""
    sets = []
    for category in categories:
        rules = set().union(*(resolved[n] for n in category.geosite))
        sets.append((category, {r.value for r in rules if r.kind == "domain"}, {r.value for r in rules if r.kind == "full"}))

    def inside(suffixes: set[str], fulls: set[str], other_suffixes: set[str], other_fulls: set[str]) -> set[str]:
        return {d for d in suffixes | fulls if covered(d, other_suffixes) or d in other_fulls}

    lines = []
    for i, (a, a_suffixes, a_fulls) in enumerate(sets):
        for b, b_suffixes, b_fulls in sets[i + 1:]:
            in_a = inside(b_suffixes, b_fulls, a_suffixes, a_fulls)
            in_b = inside(a_suffixes, a_fulls, b_suffixes, b_fulls)
            if not in_a and not in_b or len(in_a) == len(b_suffixes | b_fulls) or len(in_b) == len(a_suffixes | a_fulls):
                continue  # общая категория вроде «Все российские сайты» целиком включает другую — так и задумано
            shared = sorted(in_a | in_b, key=domain_key)
            more = f" и ещё {len(shared) - 5}" if len(shared) > 5 else ""
            lines.append(f"  {a.id} ← {b.id}: {', '.join(shared[:5])}{more}")
    return lines


# ---------- sing-box ----------

def sing_box(binary: str, *args: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run([binary, "--disable-color", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise BuildError(f"не найден sing-box «{binary}» — укажите путь: --sing-box путь/к/sing-box") from e
    if result.returncode != 0:
        raise BuildError(f"sing-box {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result


def write_rule_set(out: Path, tag: str, rule: dict, text_lines: list[str], binary: str) -> Path:
    source = out / "json" / f"{tag}.json"
    source.write_text(json.dumps({"version": RULE_SET_VERSION, "rules": [rule]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "text" / f"{tag}.txt").write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    compiled = out / f"{tag}.srs"
    sing_box(binary, "rule-set", "compile", "--output", str(compiled), str(source))
    return compiled


def run_tests(path: Path, categories: list[Category], compiled: dict[str, Path], binary: str, problems: Problems) -> int:
    """Строки «<домен или IP> <категория или список>»; «!» перед именем — не должен попадать."""
    by_id = {c.id: c for c in categories}
    count = 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        where = f"{rel(path)}:{number}"
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            problems.error(where, "нужно «<домен или IP> <категория или список>»")
            continue
        value, target = parts
        expected = not target.startswith("!")
        target = target.removeprefix("!")
        try:
            ipaddress.ip_address(value)
            kind = "geoip"
        except ValueError:
            kind = "geosite"
        tags = [t for t in by_id[target].rule_sets if t.startswith(kind + "-")] if target in by_id else [f"{kind}-{target}"]
        if any(t not in compiled for t in tags):
            problems.error(where, f"нет категории или списка «{target}»")
            continue
        found = any(re.search(r"^match\b", sing_box(binary, "rule-set", "match", "-f", "binary", str(compiled[t]), value).stderr, re.M) for t in tags)
        if found != expected:
            problems.error(where, f"{value} {'не попал' if expected else 'попал'} в «{target}»")
        count += 1
    return count


# ---------- сборка ----------

def build(out: Path, binary: str, ripe: str) -> None:
    problems = Problems()
    if out == ROOT or ROOT.is_relative_to(out) or (out / ".git").exists():
        raise BuildError(f"--out {out}: папка будет очищена — укажите отдельную, например build")
    shutil.rmtree(out, ignore_errors=True)
    (out / "json").mkdir(parents=True)
    (out / "text").mkdir()

    site_lists = load_site_lists(ROOT / "domains", problems)
    resolved = resolve(site_lists, problems)
    ip_lists, ripe_date = load_ip_lists(ROOT / "ip", ripe, problems)
    categories = load_categories(ROOT / "categories.json", site_lists, ip_lists, problems)
    problems.check()

    compiled: dict[str, Path] = {}
    counts: dict[str, int] = {}
    for name in sorted(site_lists):
        rule = compact(resolved[name])
        if not rule:
            problems.error(site_lists[name].where, "пустой список")
            continue
        lines = [PREFIXES[field_name] + value for field_name, values in rule.items() for value in values]
        tag = f"geosite-{name}"
        compiled[tag] = write_rule_set(out, tag, rule, lines, binary)
        counts[tag] = len(lines)
    for name, networks in sorted(ip_lists.items()):
        tag = f"geoip-{name}"
        lines = [str(n) for n in networks]
        compiled[tag] = write_rule_set(out, tag, {"ip_cidr": lines}, lines, binary)
        counts[tag] = len(lines)
    problems.check()

    tests = run_tests(ROOT / "tests" / "match.txt", categories, compiled, binary, problems)
    problems.check()

    now = datetime.now(timezone.utc)
    manifest = {
        "schema": 1,
        "version": now.strftime("%Y%m%d%H%M%S"),  # он же тег GitHub Release
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"ripe": ripe_date},
        "categories": [
            {"id": c.id, "title": c.title, "description": c.description, "rule_sets": c.rule_sets, **({"android": c.android} if c.android else {})}
            for c in categories
        ],
        "rule_sets": {
            tag: {
                "type": "ip" if tag.startswith("geoip-") else "domain",
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
                "rules": counts[tag],
            }
            for tag, path in sorted(compiled.items())
        },
    }
    (out / "index.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Готово: rule-set'ов — {len(compiled)}, категорий — {len(categories)}, проверок — {tests}, всё сходится. RIPE от {ripe_date}.")
    for category in categories:
        sizes = ", ".join(f"{t} (правил: {counts[t]}, {manifest['rule_sets'][t]['size'] // 1024 or 1} КБ)" for t in category.rule_sets)
        print(f"  {category.id:12} {category.title} — {sizes}")
    shared = overlaps(categories, resolved)
    if shared:
        print("Сайты сразу в двух категориях — сработает та, что выше в categories.json:")
        print("\n".join(shared))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Сборка rule-set'ов sing-box из domains/, ip/ и categories.json")
    parser.add_argument("--out", type=Path, default=ROOT / "build", help="куда класть результат (папка очищается), по умолчанию build/")
    parser.add_argument("--sing-box", default="sing-box", help="путь к sing-box, по умолчанию ищется в PATH")
    parser.add_argument("--ripe", default=RIPE_URL, help="статистика RIPE NCC: ссылка или файл")
    args = parser.parse_args()
    try:
        build(args.out.resolve(), args.sing_box, args.ripe)
    except BuildError as e:
        sys.exit(f"Сборка не удалась: {e}")


if __name__ == "__main__":
    main()
