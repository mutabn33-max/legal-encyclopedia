import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


OBSIDIAN_ROOT = Path(os.environ.get(
    "LEGAL_OBSIDIAN_ROOT",
    r"C:\Users\FW\Desktop\الموسوعة       القانونية\01-الأنظمة",
))
REPOSITORY = Path(os.environ.get(
    "LEGAL_SITE_REPOSITORY",
    str(Path.home() / "legal-encyclopedia-sync"),
))
DATA_FILE = REPOSITORY / "data.json"
CHECK_EVERY_SECONDS = 3


SECTION_ALIASES = {
    "نص المادة": ("نص المادة", "نص المادة النظامي"),
    "المعنى والأحكام": ("المعنى والأحكام", "شرح المادة"),
    "التطبيق العملي": ("التطبيق العملي", "كيفية استخدام المادة"),
    "حالات تطبيقية": ("حالات تطبيقية", "أمثلة تطبيقية"),
    "الشرح الميسّر": ("الشرح الميسّر", "شرح بالعامية"),
}


def section(sections, canonical):
    for name in SECTION_ALIASES[canonical]:
        value = sections.get(name, "").strip()
        if value:
            return value
    return ""


def markup(value):
    return "".join(
        "<p>" + html.escape(part).replace("\n", "<br>") + "</p>"
        for part in re.split(r"\n\s*\n", value.strip())
        if part.strip()
    )



def metadata(raw):
    """Read the two text properties written by Obsidian, without dependencies."""
    match = re.match(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", raw, re.S)
    result = {}
    if not match:
        return result
    for line in match.group(1).splitlines():
        field = re.match(r"^(الباب|عنوان المادة):[ \t]*(.*)$", line)
        if not field:
            continue
        key, value = field.groups()
        value = value.strip()
        if value.startswith('"'):
            value, _ = json.JSONDecoder().raw_decode(value)
        elif value.startswith("'"):
            quoted = re.match(r"^'((?:[^']|'')*)'(?:\s+#.*)?\s*$", value)
            if not quoted:
                raise ValueError("قيمة خاصية غير صحيحة: " + key)
            value = quoted.group(1).replace("''", "'")
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if value in ("null", "~"):
                value = ""
            if value in ("|", ">", "|-", ">-") or value.startswith(("[", "{")):
                raise ValueError("اكتب الخاصية كنص في سطر واحد: " + key)
        result[key] = value.strip()
    if result.get("الباب", "").strip("() ").strip() == "بدون باب":
        result["الباب"] = ""
    return result


def read_content(root):
    systems = []
    skipped = []
    for folder in sorted(root.iterdir(), key=lambda item: item.name):
        if not folder.is_dir() or folder.name.startswith("."):
            continue

        system_id = "s-" + hashlib.sha256(folder.name.encode("utf-8")).hexdigest()[:12]
        articles = []
        for file in sorted(folder.rglob("*.md"), key=lambda item: str(item)):
            match = re.fullmatch(r"المادة\s*[-_ ]?\s*(\d+)", file.stem)
            if not match:
                continue

            raw = file.read_text(encoding="utf-8-sig")
            properties = metadata(raw)
            parts = re.split(r"^##\s+(.+?)\s*$", raw, flags=re.MULTILINE)
            sections = dict(zip(parts[1::2], parts[2::2]))
            legal_text = section(sections, "نص المادة")
            if not legal_text:
                skipped.append(str(file))
                continue

            cases_raw = section(sections, "حالات تطبيقية")
            case_parts = re.split(r"^###\s+(.+?)\s*$", cases_raw, flags=re.MULTILINE)
            cases = [
                {"title": html.escape(title), "body": markup(body)}
                for title, body in zip(case_parts[1::2], case_parts[2::2])
                if body.strip()
            ]
            if not cases and cases_raw:
                cases = [{"title": "حالات تطبيقية", "body": markup(cases_raw)}]

            practical = section(sections, "التطبيق العملي")
            articles.append({
                "num": int(match.group(1)),
                "title": html.escape(properties.get("عنوان المادة", "")),
                "chapter": html.escape(properties.get("الباب", "")),
                "text": markup(legal_text),
                "explain": markup(section(sections, "المعنى والأحكام")),
                "usage": [markup(practical)] if practical else [],
                "examples": cases,
                "slang": markup(section(sections, "الشرح الميسّر")),
                "related": [],
                "added": datetime.fromtimestamp(file.stat().st_mtime).strftime("%Y-%m-%d"),
            })

        if articles:
            numbers = [article["num"] for article in articles]
            if len(numbers) != len(set(numbers)):
                raise ValueError(f"يوجد رقمان مكرران للمواد داخل: {folder}")
            systems.append({
                "id": system_id,
                "name": html.escape(folder.name),
                "type": "لائحة" if "لائحة" in folder.name else "نظام",
                "desc": "",
                "articles": sorted(articles, key=lambda article: article["num"]),
            })

    return systems, skipped


def run_git(*arguments, check=True):
    return subprocess.run(
        ["git", *arguments],
        cwd=REPOSITORY,
        text=True,
        check=check,
    )


def generate_data():
    systems, skipped = read_content(OBSIDIAN_ROOT)
    if not systems:
        raise ValueError("لم أجد مواد مكتملة داخل مجلد الأنظمة.")

    content = json.dumps(systems, ensure_ascii=False, indent=2) + "\n"
    previous = DATA_FILE.read_text(encoding="utf-8") if DATA_FILE.exists() else ""
    if content == previous:
        return False, systems, skipped

    temporary = DATA_FILE.with_suffix(".json.tmp")
    temporary.write_text(content, encoding="utf-8")
    json.loads(temporary.read_text(encoding="utf-8"))
    temporary.replace(DATA_FILE)
    return True, systems, skipped


def publish():
    run_git("pull", "--rebase")
    changed, systems, skipped = generate_data()
    article_count = sum(len(system["articles"]) for system in systems)
    if not changed:
        result = run_git("push", "origin", "main", check=False)
        if result.returncode != 0:
            print("تعذر الرفع الآن. ستتم إعادة المحاولة تلقائيًا.", flush=True)
            return False
        print(f"لا يوجد تغيير — عدد المواد المنشورة: {article_count}", flush=True)
        return True

    print(f"تم تجهيز {article_count} مادة. جارٍ رفعها للموقع...", flush=True)
    for file in skipped:
        print(f"تم تجاوز ملف غير مكتمل: {file}", flush=True)

    run_git("add", "--", "data.json")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", "data.json"],
        cwd=REPOSITORY,
    )
    if staged.returncode == 0:
        return True

    run_git("commit", "-m", "تحديث مواد الموسوعة من Obsidian")
    result = run_git("push", "origin", "main", check=False)
    if result.returncode != 0:
        print("تعذر الرفع الآن. سيُعاد تلقائيًا عند التعديل القادم.", flush=True)
        return False

    print("تم رفع التحديث. يظهر في الموقع خلال دقائق قليلة.", flush=True)
    return True


def source_signature():
    digest = hashlib.sha256()
    for file in sorted(OBSIDIAN_ROOT.rglob("*.md"), key=lambda item: str(item)):
        try:
            digest.update(str(file).encode("utf-8"))
            digest.update(file.read_bytes())
        except OSError:
            pass
    return digest.hexdigest()


def validate_paths():
    if not OBSIDIAN_ROOT.is_dir():
        raise ValueError(f"مجلد Obsidian غير موجود:\n{OBSIDIAN_ROOT}")
    if not (REPOSITORY / ".git").is_dir():
        raise ValueError(f"مجلد الموقع المرتبط بـ GitHub غير موجود:\n{REPOSITORY}")
    if not (REPOSITORY / "index.html").is_file():
        raise ValueError(f"ملف index.html غير موجود داخل:\n{REPOSITORY}")


def configure_git_identity():
    subprocess.run(
        ["git", "config", "user.name", "mutabn33-max"],
        cwd=REPOSITORY,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "mutabn33-max@users.noreply.github.com"],
        cwd=REPOSITORY,
        check=True,
    )


def main():
    validate_paths()
    configure_git_identity()
    print("مراقبة مواد Obsidian بدأت. أبقِ هذه النافذة مفتوحة.", flush=True)
    print(f"المصدر: {OBSIDIAN_ROOT}", flush=True)
    print(f"الموقع: {REPOSITORY}", flush=True)

    last_signature = None
    retry_needed = True
    while True:
        current_signature = source_signature()
        if current_signature != last_signature or retry_needed:
            try:
                retry_needed = not publish()
                if not retry_needed:
                    last_signature = current_signature
            except Exception as error:
                retry_needed = True
                print(f"خطأ: {error}", flush=True)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nتم إيقاف المزامنة.")
    except Exception as error:
        print(f"خطأ: {error}")
        input("اضغط Enter للإغلاق...")
