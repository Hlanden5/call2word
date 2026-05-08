"""CPU-only video transcription GUI built with PySide6 and faster-whisper."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import imageio_ffmpeg
from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "VideoTranscriber"
APP_VERSION = "1.1.8"
SUPPORTED_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}
TELEMOST_PREFIX = "Встреча в Телемосте"
MODELS = ["large-v3-turbo", "large-v3"]
SPEED_MODES = {
    "Быстро": {
        "beam_size": 1,
        "condition_on_previous_text": False,
        "description": "максимальная скорость, качество чуть ниже",
    },
    "Баланс": {
        "beam_size": 3,
        "condition_on_previous_text": False,
        "description": "быстрее старого режима, обычно нормально для созвонов",
    },
    "Качество": {
        "beam_size": 5,
        "condition_on_previous_text": True,
        "description": "медленнее, но аккуратнее на сложной речи",
    },
}
DEFAULT_TEMPLATE = """Ты — редактор и аналитик созвонов. Ниже одна непрерывная расшифровка видео на русском языке.

Сделай структурированный результат в виде валидного JSON. Верни только JSON, без markdown и пояснений вокруг.

Важно про входной текст:
- Это сырой поток речи, а не готовый план документа.
- Переносы строк и абзацы в расшифровке нужны только для читаемости.
- Не создавай блоки по порядку абзацев, по длине текста или по соседним кускам речи.
- Сначала определи реальные смысловые темы обсуждения, затем сгруппируй текст в блоки по этим темам.
- Один смысловой блок может собирать детали из разных мест расшифровки.
- Если тема повторялась несколько раз, объедини ее в один блок, а не создавай дубликаты.

Схема:
{
  "title": "короткое название созвона",
  "short_summary": "резюме в 3-5 предложениях",
  "blocks": [
    {
      "number": 1,
      "title": "название смыслового блока",
      "text": "что обсуждали в этом блоке, 3-7 предложений",
      "decisions": ["решение или вывод, если есть"],
      "tasks": [
        {"who": null, "what": "что сделать", "deadline": null}
      ],
      "open_questions": ["вопрос, который остался открытым"],
      "risks": ["риск, спорный момент или неуверенно распознанное место"]
    }
  ],
  "global_tasks": [
    {"who": null, "what": "что сделать", "deadline": null}
  ],
  "unclear_or_risky": ["важные места, где расшифровка может быть неточной"]
}

Правила:
- Делай столько блоков, сколько реально нужно по смыслу: не растягивай искусственно.
- Название блока должно быть смысловым, например "Сертификация и защита персональных данных", а не "Часть 1" или "Обсуждение".
- Если данных для поля нет, ставь пустой массив [] или null.
- Если смысл фразы неразборчив, но можно восстановить по контексту — восстанови, но добавь в поле "risks" запись вида: "⚠ Додумано: '[что додумано]' — потому что [причина вывода]".
- Не выдумывай имена, факты, сроки и решения, которых нет в расшифровке.
- Убирай воду и повторы, но не теряй важные детали.
- На выходе не упоминай длину расшифровки, абзацы, переносы строк или техническую подготовку текста.

Расшифровка:
{text}"""

MODEL_INFO = {
    "large-v3-turbo": {
        "ram": "~2-3 ГБ RAM",
        "disk": "~1.6 ГБ модели",
        "speed": "средне/медленно на CPU",
        "quality": "сильная модель, быстрее full large",
    },
    "large-v3": {
        "ram": "~4-5 ГБ RAM",
        "disk": "~3 ГБ модели",
        "speed": "очень медленно на CPU",
        "quality": "лучшее качество, требует много памяти",
    },
}


def app_dir() -> Path:
    """Return the directory where runtime files should be stored."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """Return a resource path for source and PyInstaller modes."""
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / name


def format_seconds(seconds: float) -> str:
    """Format seconds as mm:ss."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def default_cpu_threads() -> int:
    """Return a sensible CPU thread count for faster-whisper."""
    cores = os.cpu_count() or 4
    return max(1, cores)


def performance_cpu_threads() -> int:
    """Return a fast CPU thread count that avoids common oversubscription losses."""
    cores = os.cpu_count() or 4
    if cores <= 4:
        return cores
    return max(1, min(cores, round(cores * 0.75)))


def scan_video_folder(folder: Path) -> list[Path]:
    """Find supported videos recursively, newest modified files first."""
    return sorted(
        (
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ),
        key=lambda item: (-item.stat().st_mtime, item.name.lower()),
    )


def compact_transcript(text: str) -> str:
    """Mechanically reduce noisy transcript text without semantic summarization."""
    compacted = re.sub(r"\s+", " ", text).strip()
    if not compacted:
        return ""

    filler_phrases = (
        "как бы",
        "в общем",
        "в принципе",
        "короче",
        "собственно",
        "типа",
        "значит",
    )
    for phrase in filler_phrases:
        compacted = re.sub(rf"(?i)(?<!\w){re.escape(phrase)}(?!\w)[,\s]*", "", compacted)

    compacted = re.sub(r"(?i)(?<!\w)(ну|ээ+|эм+|мм+|угу|ага)(?!\w)[,\s]*", "", compacted)
    compacted = re.sub(r"(?i)\b([а-яёa-z0-9]{2,})(?:\s+\1\b)+", r"\1", compacted)
    compacted = re.sub(r"\s+([,.;:!?])", r"\1", compacted)
    compacted = re.sub(r"\s{2,}", " ", compacted)
    return compacted.strip()


def parse_llm_json(text: str) -> object | None:
    """Parse an LLM JSON answer, including common fenced-code responses."""
    stripped = text.strip()
    fenced = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced)

    candidates = [stripped, fenced]
    start = fenced.find("{")
    end = fenced.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(fenced[start : end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def item_to_line(item: object) -> str:
    """Convert simple JSON values into a readable line for DOCX output."""
    if isinstance(item, dict):
        values = [str(value).strip() for value in item.values() if value not in (None, "", [])]
        return " — ".join(values)
    if isinstance(item, list):
        return ", ".join(str(value) for value in item if value not in (None, "", []))
    return str(item)


def append_json_items(lines: list[str], title: str, items: object, heading: str = "##") -> None:
    """Append a titled list from a parsed JSON value."""
    if not items:
        return
    lines.append(f"{heading} {title}")
    if isinstance(items, list):
        for item in items:
            line = item_to_line(item)
            if line:
                lines.append(f"- {line}")
    else:
        lines.append(str(items))


def format_llm_answer_for_docx(answer: str) -> str:
    """Turn JSON LLM output into a readable document body when possible."""
    parsed = parse_llm_json(answer)
    if not isinstance(parsed, dict):
        return answer

    lines: list[str] = []
    summary = parsed.get("short_summary") or parsed.get("summary")
    if summary:
        lines.append("## Краткое резюме")
        lines.append(str(summary))

    blocks = parsed.get("blocks")
    if isinstance(blocks, list) and blocks:
        lines.append("## Смысловые блоки")
        for index, block in enumerate(blocks, start=1):
            if not isinstance(block, dict):
                lines.append(f"### Блок {index}")
                lines.append(str(block))
                continue
            number = block.get("number") or block.get("block") or index
            block_title = block.get("title") or "Без названия"
            lines.append(f"### Блок {number}. {block_title}")
            if block.get("text"):
                lines.append(str(block["text"]))
            append_json_items(lines, "Решения", block.get("decisions"), "###")
            append_json_items(lines, "Задачи", block.get("tasks"), "###")
            append_json_items(lines, "Открытые вопросы", block.get("open_questions"), "###")
            append_json_items(lines, "Риски и неточности", block.get("risks"), "###")

    append_json_items(lines, "Общие задачи", parsed.get("global_tasks") or parsed.get("action_items"))
    append_json_items(lines, "Неясные или рискованные места", parsed.get("unclear_or_risky"))
    return "\n".join(lines).strip() or answer


def llm_docx_title(answer: str) -> str:
    """Return a document title from an LLM JSON answer when available."""
    parsed = parse_llm_json(answer)
    if isinstance(parsed, dict) and parsed.get("title"):
        return str(parsed["title"]).strip()
    return "Ответ LLM"


def clean_docx_line(line: str) -> tuple[str, str]:
    """Return a simple DOCX paragraph style and cleaned text for one line."""
    stripped = line.strip()
    if not stripped:
        return "Normal", ""
    if stripped.startswith("### "):
        return "Heading2", stripped[4:].strip()
    if stripped.startswith("## "):
        return "Heading1", stripped[3:].strip()
    if stripped.startswith("# "):
        return "Heading1", stripped[2:].strip()
    if stripped.startswith(("- ", "* ")):
        return "Normal", f"• {stripped[2:].strip()}"
    return "Normal", stripped.replace("**", "")


def docx_paragraph(text: str, style: str = "Normal") -> str:
    """Build a minimal Word paragraph XML fragment."""
    style_xml = "" if style == "Normal" else f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
    if not text:
        return f"<w:p>{style_xml}</w:p>"
    safe_text = escape(text)
    return (
        f"<w:p>{style_xml}<w:r>"
        '<w:t xml:space="preserve">'
        f"{safe_text}"
        "</w:t></w:r></w:p>"
    )


def write_docx(path: Path, title: str, body: str) -> None:
    """Write plain text into a small valid DOCX file without extra dependencies."""
    paragraphs = [docx_paragraph(title, "Heading1")]
    paragraphs.append(docx_paragraph(f"Создано: {datetime.now().strftime('%d.%m.%Y %H:%M')}"))
    paragraphs.append(docx_paragraph(""))
    for raw_line in body.splitlines():
        style, text = clean_docx_line(raw_line)
        paragraphs.append(docx_paragraph(text, style))

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(paragraphs)}
    <w:sectPr>
      <w:pgSz w:w="11906" w:h="16838"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>"""

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

    document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

    styles_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:qFormat/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="32"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
    <w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="26"/></w:rPr>
  </w:style>
</w:styles>"""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", root_rels)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/_rels/document.xml.rels", document_rels)
        docx.writestr("word/styles.xml", styles_xml)


def _get_duration(path: Path) -> str:
    """Return duration string 'MM:SS' for a video file.
    Tries mutagen/struct for webm, falls back to mtime-ctime delta."""
    try:
        import struct
        # Parse WebM/Matroska duration from EBML header (~fast, no deps)
        with open(path, "rb") as f:
            data = f.read(65536)
        # Find TimecodeScale and Duration in EBML
        # Duration element ID: 0x4489, float64
        idx = data.find(b"\x44\x89")
        if idx != -1 and idx + 10 < len(data):
            size_byte = data[idx + 2]
            if size_byte == 0x88:  # 8-byte double
                val = struct.unpack(">d", data[idx + 3:idx + 11])[0]
                # find TimecodeScale (default 1000000 ns = 1ms)
                scale = 1_000_000
                ts_idx = data.find(b"\x2A\xD7\xB1")
                if ts_idx != -1 and ts_idx + 6 < len(data):
                    sb = data[ts_idx + 3]
                    n_bytes = bin(sb).lstrip("0b").find("1") + 1 if sb else 4
                    n_bytes = max(1, min(n_bytes, 4))
                    scale_bytes = data[ts_idx + 3:ts_idx + 3 + n_bytes]
                    # strip VINT prefix
                    scale = int.from_bytes(scale_bytes, "big") & (0xFF >> (n_bytes - 1).bit_length())
                    if scale == 0:
                        scale = 1_000_000
                secs = val * scale / 1_000_000_000
                if 0 < secs < 86400:
                    return format_seconds(secs)
    except Exception:
        pass
    # fallback: mtime - ctime ≈ duration for Telemost recordings
    try:
        stat = path.stat()
        delta = stat.st_mtime - getattr(stat, "st_birthtime", stat.st_ctime)
        if 10 < delta < 86400:
            return format_seconds(delta)
    except Exception:
        pass
    return ""


def _parse_telemost_name(stem: str) -> tuple[str, str]:
    """Extract date and time from 'Встреча в Телемосте ДД.ММ.ГГ ЧЧ-ММ-СС — запись'.
    Returns (date_str, time_str) or ('', '') if not matched."""
    m = re.search(r"(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}-\d{2}(?:-\d{2})?)", stem)
    if not m:
        return "", ""
    date = m.group(1)
    time_raw = m.group(2).replace("-", ":")
    return date, time_raw


class FileList(QTableWidget):
    """Checkable table: Telemost mode shows Встречи/Дата/Время, generic mode shows Файл."""

    files_changed = Signal()

    _COL_CHECK = 0
    _COL_NAME  = 1
    _COL_DATE  = 2
    _COL_TIME  = 3
    _COL_DUR   = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 5, parent)
        self._syncing_checks = False
        self._telemost_mode = False
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.setToolTip("Перетащите сюда видео или папку")
        self.itemChanged.connect(self._keep_only_one_checked)
        self._set_list_mode()

    def _set_list_mode(self) -> None:
        self._telemost_mode = False
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels(["", "Файл"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.setColumnWidth(0, 28)

    def _set_telemost_mode(self) -> None:
        self._telemost_mode = True
        self.setColumnCount(5)
        self.setHorizontalHeaderLabels(["", "Встречи в Телемосте", "Дата", "Время", "Длит."])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.setColumnWidth(0, 28)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths: list[Path] = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile()).resolve()
            if path.is_dir():
                paths.extend(scan_video_folder(path))
            else:
                paths.append(path)
        self.add_files(paths)
        event.acceptProposedAction()

    def add_files(self, paths: list[Path | str]) -> int:
        valid = [
            Path(p).resolve() for p in paths
            if Path(p).suffix.lower() in SUPPORTED_EXTENSIONS and Path(p).is_file()
        ]
        if not valid:
            return 0

        # determine mode from first new file
        is_telemost = all(p.stem.startswith(TELEMOST_PREFIX) for p in valid)
        if self.rowCount() == 0:
            if is_telemost:
                self._set_telemost_mode()
            else:
                self._set_list_mode()

        existing = {self.item(r, self._COL_CHECK).data(Qt.UserRole)
                    for r in range(self.rowCount())
                    if self.item(r, self._COL_CHECK)}
        has_checked = bool(self.checked_paths())
        added = 0
        self._syncing_checks = True
        try:
            for path in valid:
                if str(path) in existing:
                    continue

                row = self.rowCount()
                self.insertRow(row)

                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Checked if not has_checked else Qt.Unchecked)
                chk.setData(Qt.UserRole, str(path))
                self.setItem(row, self._COL_CHECK, chk)

                if self._telemost_mode:
                    date_str, time_str = _parse_telemost_name(path.stem)
                    label = path.stem.replace(TELEMOST_PREFIX, "").strip(" —")
                    name_item = QTableWidgetItem(label)
                    name_item.setData(Qt.UserRole, str(path))
                    name_item.setToolTip(str(path))
                    self.setItem(row, self._COL_NAME, name_item)
                    self.setItem(row, self._COL_DATE, QTableWidgetItem(date_str))
                    self.setItem(row, self._COL_TIME, QTableWidgetItem(time_str))
                    self.setItem(row, self._COL_DUR,  QTableWidgetItem(_get_duration(path)))
                else:
                    name_item = QTableWidgetItem(path.name)
                    name_item.setData(Qt.UserRole, str(path))
                    name_item.setToolTip(str(path))
                    self.setItem(row, self._COL_NAME, name_item)

                existing.add(str(path))
                has_checked = True
                added += 1
        finally:
            self._syncing_checks = False
        if added:
            self.files_changed.emit()
        return added

    def checked_paths(self) -> list[str]:
        for row in range(self.rowCount()):
            chk = self.item(row, self._COL_CHECK)
            if chk and chk.checkState() == Qt.Checked:
                return [chk.data(Qt.UserRole)]
        return []

    def set_all_checked(self, checked: bool) -> None:
        self._syncing_checks = True
        try:
            for row in range(self.rowCount()):
                chk = self.item(row, self._COL_CHECK)
                if chk:
                    chk.setCheckState(Qt.Checked if checked and row == 0 else Qt.Unchecked)
        finally:
            self._syncing_checks = False
        self.files_changed.emit()

    def _keep_only_one_checked(self, changed_item: QTableWidgetItem) -> None:
        if self._syncing_checks or changed_item.column() != self._COL_CHECK:
            return
        if changed_item.checkState() != Qt.Checked:
            self.files_changed.emit()
            return
        self._syncing_checks = True
        try:
            for row in range(self.rowCount()):
                chk = self.item(row, self._COL_CHECK)
                if chk and chk is not changed_item:
                    chk.setCheckState(Qt.Unchecked)
        finally:
            self._syncing_checks = False
        self.files_changed.emit()


class TranscribeWorker(QThread):
    """Background worker that extracts audio and transcribes it on CPU."""

    progress = Signal(str)
    progress_value = Signal(int)
    file_done = Signal(str, str)
    all_done = Signal()
    error = Signal(str)

    def __init__(
        self,
        files: list[str],
        model_size: str,
        cpu_threads: int,
        speed_mode: str,
        vad_filter: bool,
    ) -> None:
        """Store immutable worker parameters before the thread starts."""
        super().__init__()
        self.files = files
        self.model_size = model_size
        self.cpu_threads = max(1, int(cpu_threads))
        self.speed_mode = speed_mode if speed_mode in SPEED_MODES else "Быстро"
        self.vad_filter = vad_filter
        self._cancel_requested = False

    def cancel(self) -> None:
        """Ask the worker to stop after the current operation."""
        self._cancel_requested = True

    def run(self) -> None:
        """Load Whisper once, then process all checked files."""
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # pragma: no cover - depends on local env
            self.error.emit(f"Не удалось импортировать faster-whisper: {exc}")
            self.all_done.emit()
            return

        try:
            info = MODEL_INFO[self.model_size]
            os.environ["OMP_NUM_THREADS"] = str(self.cpu_threads)
            os.environ["MKL_NUM_THREADS"] = str(self.cpu_threads)
            self.progress.emit(
                f"Загрузка модели {self.model_size}: {info['ram']}, CPU/int8, потоков CPU: {self.cpu_threads}. "
                "При первом запуске модель скачивается из HuggingFace."
            )
            model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                cpu_threads=self.cpu_threads,
                num_workers=4,
            )
        except MemoryError:
            self.error.emit("Недостаточно памяти для модели. Попробуйте small, base или tiny.")
            self.all_done.emit()
            return
        except Exception as exc:
            self.error.emit(
                "Модель не загрузилась. Проверьте интернет для первого скачивания, "
                f"доступ к HuggingFace cache и свободное место на диске. Детали: {exc}"
            )
            self.all_done.emit()
            return

        for index, file_path in enumerate(self.files, start=1):
            if self._cancel_requested:
                self.progress.emit("Остановлено пользователем.")
                break
            self._process_one(model, file_path, index, len(self.files))

        self.progress_value.emit(100)
        self.all_done.emit()

    def _process_one(self, model, file_path: str, index: int, total: int) -> None:
        """Extract one video to WAV and run Whisper transcription."""
        path = Path(file_path)
        started = time.monotonic()
        self.progress_value.emit(0)
        self.progress.emit(f"[{index}/{total}] {path.name}")

        try:
            with tempfile.TemporaryDirectory(prefix="video_transcriber_") as tmp_dir:
                audio_path = Path(tmp_dir) / "audio.wav"
                self._extract_audio(path, audio_path)
                self._denoise_audio(audio_path)
                text, audio_duration = self._transcribe_audio(model, audio_path)
                elapsed = format_seconds(time.monotonic() - started)
                elapsed_raw = max(time.monotonic() - started, 0.1)
                realtime = audio_duration / elapsed_raw if audio_duration else 0.0
                self.progress.emit(
                    f"Готово: {path.name}, время обработки {elapsed}, скорость {realtime:.2f}x realtime"
                )
                self.file_done.emit(str(path), text)
        except MemoryError:
            self.error.emit(f"{path.name}: не хватает RAM. Выберите модель меньше.")
        except subprocess.CalledProcessError as exc:
            details = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
            self.error.emit(f"{path.name}: ffmpeg не смог извлечь аудио. {details.strip()}")
        except FileNotFoundError:
            self.error.emit("ffmpeg not found. imageio-ffmpeg должен был поставить бинарь автоматически.")
        except Exception as exc:
            self.error.emit(f"{path.name}: ошибка обработки: {exc}")

    def _denoise_audio(self, audio_path: Path) -> None:
        """Apply highpass + stationary noisereduce in-place (COMBINED preset)."""
        try:
            import soundfile as sf
            import noisereduce as nr
            from scipy.signal import butter, sosfilt
        except ImportError:
            self.progress.emit("Деноизинг пропущен: установите soundfile noisereduce scipy")
            return

        self.progress.emit("Деноизинг: highpass + noisereduce stationary...")
        data, sr = sf.read(str(audio_path), dtype="float32")
        sos = butter(4, 80, btype="high", fs=sr, output="sos")
        data = sosfilt(sos, data).astype("float32")
        data = nr.reduce_noise(y=data, sr=sr, stationary=True, prop_decrease=0.8)
        sf.write(str(audio_path), data, sr)

    def _extract_audio(self, video_path: Path, audio_path: Path) -> None:
        """Create a 16 kHz mono WAV file via the bundled ffmpeg binary."""
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        self.progress.emit("Извлечение аудио: PCM 16 kHz mono WAV...")
        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio_path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _transcribe_audio(self, model, audio_path: Path) -> tuple[str, float]:
        """Transcribe Russian audio and return clean plain text."""
        options = SPEED_MODES[self.speed_mode]
        self.progress.emit(
            "Транскрипция: "
            f"режим={self.speed_mode}, beam_size={options['beam_size']}, "
            f"language=ru, vad_filter={self.vad_filter}..."
        )
        segments, info = model.transcribe(
            str(audio_path),
            language="ru",
            beam_size=options["beam_size"],
            vad_filter=self.vad_filter,
            vad_parameters={"threshold": 0.5, "min_silence_duration_ms": 500},
            condition_on_previous_text=options["condition_on_previous_text"],
            without_timestamps=False,
            initial_prompt="Транскрипция рабочего созвона на русском языке.",
            temperature=0.0,
            repetition_penalty=1.1,
        )
        duration = max(float(getattr(info, "duration", 0.0) or 0.0), 1.0)
        rows: list[dict[str, float | str]] = []

        for segment in segments:
            if self._cancel_requested:
                break
            text = segment.text.strip()
            if text:
                rows.append({"start": float(segment.start), "end": float(segment.end), "text": text})
            percent = min(99, int((float(segment.end) / duration) * 100))
            self.progress_value.emit(percent)

        self.progress_value.emit(100)
        return " ".join(str(row["text"]) for row in rows).strip(), duration


class MainWindow(QMainWindow):
    """Main application window for queueing files and building prompts."""

    def __init__(self) -> None:
        """Build UI, load settings, and prepare runtime state."""
        super().__init__()
        self.worker: TranscribeWorker | None = None
        self.transcripts: dict[str, str] = {}
        self.current_folder: Path | None = None
        self.started_at: float | None = None
        self.settings = QSettings(str(app_dir() / f"{APP_NAME}.ini"), QSettings.IniFormat)
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self._tick_elapsed)

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        icon = resource_path("icon.png")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.setMinimumSize(840, 600)
        self.resize(1000, 700)

        self._build_ui()
        self._load_settings()
        self._update_model_info()
        self._update_status("Готово")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt method name
        """Persist settings and stop the worker on window close."""
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(event)

    def _build_ui(self) -> None:
        """Create a compact but more expressive desktop interface."""
        self.file_list = FileList()
        self.model_combo = QComboBox()
        self.model_combo.addItems(MODELS)
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(list(SPEED_MODES))
        self.cpu_threads_spin = QSpinBox()
        self.cpu_threads_spin.setRange(1, max(1, os.cpu_count() or 1))
        self.cpu_threads_spin.setValue(default_cpu_threads())
        self.cpu_threads_spin.setSuffix(" потоков")
        self.cpu_threads_spin.setToolTip("Сколько потоков CPU отдавать faster-whisper. Больше потоков = быстрее, но компьютер будет сильнее занят.")

        self.model_info_label = QLabel()
        self.model_info_label.setObjectName("modelInfo")
        self.model_info_label.setWordWrap(True)


        self.vad_check = QCheckBox("VAD паузы")
        self.vad_check.setToolTip("Обрезает тишину. Помогает, если много пауз; на непрерывном созвоне может быть медленнее.")
        self.compact_prompt_check = QCheckBox("Сжимать расшифровку для LLM")
        self.auto_save_check = QCheckBox("Автосохранять промт в .txt")
        self.save_dir_edit = QLineEdit()
        self.save_dir_edit.setVisible(False)
        self.save_dir_button = QPushButton("Папка")
        self.save_dir_button.setVisible(False)

        self.open_folder_button = QPushButton("Открыть папку")
        self.run_button = QPushButton("Запустить")
        self.clear_button = QPushButton("Очистить")
        self.copy_button = QPushButton("Копировать промт")
        self.save_button = QPushButton("Сохранить промт")
        self.paste_llm_button = QPushButton("Вставить из буфера")
        self.save_word_button = QPushButton("Сохранить Word")
        self.check_all_button = QPushButton("Последний")
        self.uncheck_all_button = QPushButton("Ничего")
        self.check_all_button.setFixedWidth(96)
        self.uncheck_all_button.setFixedWidth(76)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)

        self.file_count_label = QLabel("0 файлов")
        self.checked_count_label = QLabel("0 файл")
        self.elapsed_label = QLabel("00:00")

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.template_edit = QTextEdit()
        self.template_edit.setPlaceholderText("Шаблон должен содержать {text}")
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setReadOnly(True)
        self.llm_edit = QTextEdit()
        self.llm_edit.setPlaceholderText("Вставьте сюда ответ LLM и нажмите «Сохранить Word».")

        header = self._header()
        left_panel = self._left_panel()
        right_panel = self._right_panel()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([390, 610])

        central = QWidget()
        central.setObjectName("root")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)
        layout.addWidget(header, 0)
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)

        self.open_folder_button.clicked.connect(self._choose_folder)
        self.run_button.clicked.connect(self._start)
        self.clear_button.clicked.connect(self._clear_files)
        self.copy_button.clicked.connect(self._copy_prompt)
        self.save_button.clicked.connect(lambda: self._save_prompt_file(manual=True))
        self.paste_llm_button.clicked.connect(self._paste_llm_answer)
        self.save_word_button.clicked.connect(self._save_word_file)

        self.save_dir_button.clicked.connect(self._choose_save_dir)
        self.check_all_button.clicked.connect(lambda: self.file_list.set_all_checked(True))
        self.uncheck_all_button.clicked.connect(lambda: self.file_list.set_all_checked(False))
        self.file_list.files_changed.connect(lambda: self._update_status("Очередь обновлена"))
        self.file_list.itemChanged.connect(lambda _item: self._update_status("Выбор файлов обновлен"))
        self.model_combo.currentTextChanged.connect(self._update_model_info)
        self.speed_combo.currentTextChanged.connect(self._update_model_info)
        self.cpu_threads_spin.valueChanged.connect(self._update_model_info)
        self.vad_check.stateChanged.connect(self._update_model_info)
        self.template_edit.textChanged.connect(self._refresh_prompt)
        self.compact_prompt_check.stateChanged.connect(self._refresh_prompt)

        folder_action = QAction("Открыть папку", self)
        folder_action.triggered.connect(self._choose_folder)
        self.addAction(folder_action)

        self.setStyleSheet(self._style())

    def _header(self) -> QFrame:
        """Build the top title and quick stats strip."""
        frame = QFrame()
        frame.setObjectName("header")
        frame.setFixedHeight(96)
        frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 12, 16, 12)

        title_box = QVBoxLayout()
        title = QLabel("VideoTranscriber")
        title.setObjectName("title")
        subtitle = QLabel("Видео → русский текст → готовый промт")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        layout.addLayout(title_box)
        layout.addStretch(1)
        layout.addWidget(self._metric("В папке", self.file_count_label))
        layout.addWidget(self._metric("К обработке", self.checked_count_label))
        layout.addWidget(self._metric("Время", self.elapsed_label))
        return frame

    def _metric(self, caption: str, value: QLabel) -> QFrame:
        """Create a small non-editable metric tile."""
        frame = QFrame()
        frame.setObjectName("metric")
        frame.setMinimumWidth(104)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 7, 10, 7)
        label = QLabel(caption)
        label.setObjectName("metricCaption")
        value.setObjectName("metricValue")
        layout.addWidget(label)
        layout.addWidget(value)
        return frame

    def _left_panel(self) -> QFrame:
        """Build the file queue and processing controls."""
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(9)

        title = QLabel("Видео из папки")
        title.setObjectName("sectionTitle")
        hint = QLabel("Откройте папку или перетащите сюда видео/папку. Сейчас за запуск обрабатывается один файл с галочкой.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")

        file_buttons = QHBoxLayout()
        file_buttons.setSpacing(8)
        file_buttons.addWidget(self.open_folder_button, 1)
        file_buttons.addWidget(self.check_all_button)
        file_buttons.addWidget(self.uncheck_all_button)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addLayout(file_buttons)
        layout.addWidget(self.file_list, 1)

        model_title = QLabel("Модель и режим")
        model_title.setObjectName("sectionTitle")
        threads_row = QHBoxLayout()
        threads_row.addWidget(QLabel("CPU потоков"))
        threads_row.addWidget(self.cpu_threads_spin)

        layout.addWidget(model_title)
        layout.addLayout(threads_row)
        layout.addWidget(self.model_info_label)
        layout.addWidget(self.compact_prompt_check)
        layout.addWidget(self.auto_save_check)


        action_row = QHBoxLayout()
        action_row.addWidget(self.run_button)
        action_row.addWidget(self.clear_button)
        layout.addLayout(action_row)
        layout.addWidget(self.progress_bar)
        return panel

    def _right_panel(self) -> QFrame:
        """Build prompt, rendered output, and log tabs."""
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)

        tabs = QTabWidget()
        tabs.addTab(self._tab_page(self.prompt_edit), "Готовый промт")
        tabs.addTab(self._llm_tab_page(), "Ответ LLM → Word")
        tabs.addTab(self._tab_page(self.template_edit), "Шаблон")
        tabs.addTab(self._tab_page(self.log_edit), "Лог")

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.copy_button)
        button_row.addWidget(self.save_button)

        layout.addWidget(tabs, 1)
        layout.addLayout(button_row)
        return panel

    def _tab_page(self, widget: QWidget) -> QWidget:
        """Wrap a tab widget with consistent spacing."""
        page = QWidget()
        page.setObjectName("tabPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(widget)
        return page

    def _llm_tab_page(self) -> QWidget:
        """Build the manual LLM answer to DOCX tab."""
        page = QWidget()
        page.setObjectName("tabPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        hint = QLabel("Скопируйте сюда ответ любой LLM. Если это JSON по шаблону, Word будет собран в читаемые разделы.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.paste_llm_button)
        button_row.addWidget(self.save_word_button)
        layout.addWidget(hint)
        layout.addWidget(self.llm_edit, 1)
        layout.addLayout(button_row)
        return page

    def _load_settings(self) -> None:
        """Load model, prompt template, and save options."""
        settings_version = str(self.settings.value("settings_version", ""))
        # model, speed and VAD are fixed — not user-configurable
        self.model_combo.setCurrentText("large-v3-turbo")
        self.speed_combo.setCurrentText("Баланс")
        self.vad_check.setChecked(True)
        threads = self.settings.value("cpu_threads", default_cpu_threads(), type=int)
        max_threads = self.cpu_threads_spin.maximum()
        self.cpu_threads_spin.setValue(min(max(1, int(threads)), max_threads))

        stored_template = self.settings.value("template", "")
        if self._looks_like_old_default(stored_template, settings_version):
            stored_template = DEFAULT_TEMPLATE
        self.template_edit.setPlainText(stored_template or DEFAULT_TEMPLATE)

        self.compact_prompt_check.setChecked(self.settings.value("compact_prompt", True, type=bool))
        self.auto_save_check.setChecked(self.settings.value("auto_save", True, type=bool))
        saved_dir = self.settings.value("save_dir", "")
        if not saved_dir:
            try:
                profiles_root = Path(os.environ.get("SYSTEMDRIVE", "C:")) / "Users"
                for user_dir in sorted(profiles_root.iterdir()):
                    candidate = user_dir / "Documents" / "Телемост"
                    if candidate.is_dir():
                        saved_dir = str(candidate)
                        break
            except Exception:
                pass
        self.save_dir_edit.setText(saved_dir)
        if saved_dir and Path(saved_dir).is_dir():
            QTimer.singleShot(0, lambda: self._open_folder(Path(saved_dir)))
        self._refresh_prompt()

    def _save_settings(self) -> None:
        """Persist settings next to the executable/source file."""
        self.settings.setValue("cpu_threads", self.cpu_threads_spin.value())
        self.settings.setValue("template", self.template_edit.toPlainText())
        self.settings.setValue("compact_prompt", self.compact_prompt_check.isChecked())
        self.settings.setValue("auto_save", self.auto_save_check.isChecked())
        self.settings.setValue("save_dir", self.save_dir_edit.text())
        self.settings.setValue("settings_version", APP_VERSION)
        self.settings.sync()

    def _looks_like_old_default(self, template: object, settings_version: str) -> bool:
        """Detect the initial minimal template and mojibake copies of it."""
        text = str(template or "")
        old_speaker_template = settings_version in {"1.1.0", "1.1.1"} and (
            "Собеседник" in text or "РЎРѕР±РµСЃРµРґРЅРёРє" in text
        )
        return (
            not text
            or "Сделай краткое содержание по пунктам" in text
            or "Сделай структурированное резюме" in text
            or "Короткое содержание в 5-7 пунктах" in text
            or "разбита на фрагменты" in text
            or old_speaker_template
            or "РќРёР¶Рµ" in text
        )

    def _open_folder(self, folder: Path) -> None:
        self.current_folder = folder
        files = scan_video_folder(folder)
        self.file_list.clear()
        self.transcripts.clear()
        self.prompt_edit.clear()
        self.llm_edit.clear()
        added = self.file_list.add_files(files)
        self._log(f"Папка: {folder}")
        self._log(f"Найдено видео: {len(files)}, добавлено новых: {added}")
        self._update_status("Папка открыта")

    def _choose_folder(self) -> None:
        """Open a folder and add all supported videos found recursively."""
        folder = QFileDialog.getExistingDirectory(self, "Открыть папку с видео", "")
        if not folder:
            return
        self._open_folder(Path(folder).resolve())

    def _choose_save_dir(self) -> None:
        """Let the user choose where automatic prompt files are written."""
        folder = QFileDialog.getExistingDirectory(self, "Папка для сохранения промта", self.save_dir_edit.text())
        if folder:
            self.save_dir_edit.setText(folder)
            self._save_settings()

    def _start(self) -> None:
        """Start transcription for the checked file in a background QThread."""
        files = self.file_list.checked_paths()
        if not files:
            QMessageBox.information(self, APP_NAME, "Отметьте галочкой один файл для process file.")
            return

        self._save_settings()
        self.transcripts.clear()
        self.prompt_edit.clear()
        self.llm_edit.clear()
        self.progress_bar.setValue(0)
        self.started_at = time.monotonic()
        self.elapsed_label.setText("00:00")
        self.elapsed_timer.start(1000)
        self._set_running(True)
        self._log("Старт обработки.")

        self.worker = TranscribeWorker(
            files,
            self.model_combo.currentText(),
            self.cpu_threads_spin.value(),
            self.speed_combo.currentText(),
            self.vad_check.isChecked(),
        )
        self.worker.progress.connect(self._log)
        self.worker.progress_value.connect(self.progress_bar.setValue)
        self.worker.file_done.connect(self._file_done)
        self.worker.error.connect(self._error)
        self.worker.all_done.connect(self._all_done)
        self.worker.start()
        self._update_status("Идет обработка")

    def _clear_files(self) -> None:
        """Clear queue and current results."""
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Нельзя очищать список во время обработки.")
            return
        self.file_list.clear()
        self.transcripts.clear()
        self.prompt_edit.clear()
        self.llm_edit.clear()
        self.progress_bar.setValue(0)
        self.elapsed_label.setText("00:00")
        self._update_status("Список очищен")

    def _copy_prompt(self) -> None:
        """Copy the rendered prompt to the system clipboard."""
        prompt = self._render_prompt().strip()
        QApplication.clipboard().setText(prompt)
        self._log("Готовый промт скопирован в буфер обмена.")
        self._update_status("Промт скопирован")

    def _output_stem(self) -> str:
        """Return a base filename from the current source video, or timestamp fallback."""
        if self.transcripts:
            src = Path(next(iter(self.transcripts)))
            return src.stem
        return datetime.now().strftime("%Y%m%d_%H%M%S")

    def _resolve_save_dir(self) -> Path:
        """Subfolder Отчёты/<stem>/ next to source video; explicit field overrides base only."""
        stem = self._output_stem()
        if self.transcripts:
            src = Path(next(iter(self.transcripts)))
            base = self.save_dir_edit.text().strip()
            root = Path(base).expanduser() if base else src.parent / "Отчёты"
            return root / stem
        explicit = self.save_dir_edit.text().strip()
        if explicit:
            return Path(explicit).expanduser()
        return app_dir() / "outputs"

    def _save_prompt_file(self, manual: bool = False) -> Path | None:
        """Save the rendered prompt as UTF-8 text."""
        prompt = self._render_prompt().strip()
        if not prompt:
            if manual:
                QMessageBox.information(self, APP_NAME, "Промт пока пустой.")
            return None

        save_dir = self._resolve_save_dir()
        stem = self._output_stem()
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            path = save_dir / f"{stem} — расшифровка.txt"
            path.write_text(prompt, encoding="utf-8")
        except Exception as exc:
            self._error(f"Не удалось сохранить промт: {exc}")
            return None

        self._log(f"Промт сохранен: {path}")
        self._update_status("Промт сохранен")
        if manual:
            QMessageBox.information(self, APP_NAME, f"Промт сохранен:\n{path}")
        return path

    def _paste_llm_answer(self) -> None:
        """Paste an LLM answer from the clipboard into the answer editor."""
        text = QApplication.clipboard().text().strip()
        if not text:
            QMessageBox.information(self, APP_NAME, "В буфере обмена нет текста.")
            return
        self.llm_edit.setPlainText(text)
        self._log("Ответ LLM вставлен из буфера обмена.")
        self._update_status("Ответ LLM вставлен")

    def _save_word_file(self) -> Path | None:
        """Save an LLM answer as a DOCX file."""
        answer = self.llm_edit.toPlainText().strip()
        if not answer:
            QMessageBox.information(self, APP_NAME, "Вставьте ответ LLM перед сохранением Word.")
            return None

        save_dir = self._resolve_save_dir()
        stem = self._output_stem()
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
            path = save_dir / f"{stem} — отчёт.docx"
            write_docx(path, llm_docx_title(answer), format_llm_answer_for_docx(answer))
        except Exception as exc:
            self._error(f"Не удалось сохранить Word: {exc}")
            return None

        self._log(f"Word сохранен: {path}")
        self._update_status("Word сохранен")
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception:
            QMessageBox.information(self, APP_NAME, f"Word сохранен:\n{path}")
        return path

    def _file_done(self, file_path: str, text: str) -> None:
        """Store a completed transcript and refresh the prompt preview."""
        name = Path(file_path).name
        self.transcripts[file_path] = text
        self._log(f"Распознано символов для {name}: {len(text)}")
        self._refresh_prompt()

    def _error(self, message: str) -> None:
        """Append recoverable errors to the log."""
        self._log(f"ОШИБКА: {message}")
        self._update_status("Ошибка, продолжаю если возможно")

    def _all_done(self) -> None:
        """Return the GUI to idle state and save the prompt if requested."""
        self.elapsed_timer.stop()
        elapsed = format_seconds(time.monotonic() - self.started_at) if self.started_at else "00:00"
        self.elapsed_label.setText(elapsed)
        self._log(f"Все задачи завершены. Общее время: {elapsed}")
        self._set_running(False)
        self._refresh_prompt()
        if self.auto_save_check.isChecked():
            self._save_prompt_file(manual=False)
        self._update_status("Готово")

    def _refresh_prompt(self) -> None:
        """Refresh rendered prompt text."""
        self.prompt_edit.setPlainText(self._render_prompt())

    def _render_prompt(self) -> str:
        """Render the prompt template with all recognized transcripts."""
        joined = "\n\n".join(
            self._format_transcript_for_prompt(path, text) for path, text in self.transcripts.items()
        ).strip()
        template = self.template_edit.toPlainText() or DEFAULT_TEMPLATE
        if "{text}" not in template:
            template = f"{template}\n\n{{text}}"
        return template.replace("{text}", joined)

    def _format_transcript_for_prompt(self, path: str, text: str) -> str:
        """Prepare one transcript for the prompt, optionally compacting it."""
        compact = self.compact_prompt_check.isChecked()
        prepared = compact_transcript(text) if compact else re.sub(r"\s+", " ", text).strip()
        header = f"Файл: {Path(path).name}"
        if compact and len(prepared) < len(text):
            header += f"\nСжато механически: {len(text)} → {len(prepared)} символов."
        return f"{header}\n{prepared}".strip()

    def _log(self, message: str) -> None:
        """Write a timestamped message to the debug log."""
        stamp = time.strftime("%H:%M:%S")
        self.log_edit.append(f"[{stamp}] {message}")

    def _set_running(self, running: bool) -> None:
        """Enable or disable controls while the worker is active."""
        for widget in (
            self.run_button,
            self.clear_button,
            self.open_folder_button,
            self.check_all_button,
            self.uncheck_all_button,
            self.cpu_threads_spin,
            self.compact_prompt_check,
            self.auto_save_check,
        ):
            widget.setDisabled(running)

    def _tick_elapsed(self) -> None:
        """Update live elapsed processing time."""
        if self.started_at:
            self.elapsed_label.setText(format_seconds(time.monotonic() - self.started_at))

    def _update_model_info(self) -> None:
        """Show RAM, disk, speed, and quality notes for the selected model."""
        info = MODEL_INFO["large-v3-turbo"]
        speed_info = SPEED_MODES["Баланс"]
        self.model_info_label.setText(
            f"{info['ram']} | {info['disk']} | {info['speed']} | {info['quality']}. "
            f"CPU-only, int8, {self.cpu_threads_spin.value()} потоков, "
            f"баланс: {speed_info['description']}, VAD вкл."
        )

    def _update_status(self, message: str) -> None:
        """Update metrics and bottom status text."""
        total = self.file_list.rowCount()
        checked = len(self.file_list.checked_paths())
        self.file_count_label.setText(f"{total} файлов")
        self.checked_count_label.setText(f"{checked} файл")
        self.status.showMessage(f"Файлов в списке: {total} | к обработке: {checked} | {message}")

    def _style(self) -> str:
        """Return application stylesheet."""
        return """
        QMainWindow {
            background: #171a1f;
        }
        QWidget {
            color: #edf2f4;
            font-size: 13px;
        }
        QWidget#root {
            background: #171a1f;
        }
        QWidget#tabPage {
            background: transparent;
        }
        QLabel, QCheckBox {
            background: transparent;
        }
        QFrame#header {
            background: #22323a;
            border: 1px solid #38515c;
            border-radius: 8px;
        }
        QFrame#panel {
            background: #20252c;
            border: 1px solid #343d48;
            border-radius: 8px;
        }
        QFrame#metric {
            background: #172027;
            border: 1px solid #3b5963;
            border-radius: 6px;
        }
        QFrame#metric QLabel {
            background: transparent;
        }
        QLabel#title {
            font-size: 24px;
            font-weight: 800;
            color: #ffffff;
        }
        QLabel#subtitle, QLabel#hint, QLabel#metricCaption {
            color: #aab6c0;
        }
        QLabel#metricValue {
            font-size: 16px;
            font-weight: 700;
            color: #f4c95d;
        }
        QLabel#sectionTitle {
            font-size: 15px;
            font-weight: 800;
            color: #ffffff;
        }
        QLabel#modelInfo {
            background: #151a20;
            color: #cdd6df;
            border: 1px solid #3a4652;
            border-radius: 6px;
            padding: 8px;
        }
        QListWidget, QTextEdit, QComboBox, QLineEdit {
            background: #11161c;
            color: #edf2f4;
            border: 1px solid #3a4652;
            border-radius: 6px;
            padding: 5px;
            selection-background-color: #2d7f7a;
        }
        QListWidget::item {
            min-height: 26px;
            padding: 4px;
        }
        QTabWidget::pane {
            border: 0;
        }
        QTabBar::tab {
            background: #151a20;
            border: 1px solid #343d48;
            border-bottom: 0;
            padding: 8px 14px;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
        }
        QTabBar::tab:selected {
            background: #2b3b44;
            color: #ffffff;
        }
        QPushButton {
            background: #2d7f7a;
            color: #ffffff;
            border: 0;
            border-radius: 6px;
            padding: 8px 12px;
            font-weight: 700;
        }
        QPushButton:hover {
            background: #34958f;
        }
        QPushButton:pressed {
            background: #256b67;
        }
        QPushButton:disabled {
            background: #47515b;
            color: #aab4bd;
        }
        QPushButton[text="✕"] {
            background: #3a4652;
            padding: 8px 8px;
            min-width: 24px;
            max-width: 24px;
        }
        QPushButton[text="✕"]:hover {
            background: #c0392b;
        }
        QCheckBox {
            spacing: 8px;
            padding: 2px 0;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
        QProgressBar {
            border: 1px solid #3a4652;
            border-radius: 6px;
            text-align: center;
            background: #11161c;
            height: 18px;
        }
        QProgressBar::chunk {
            background: #e26d5a;
            border-radius: 5px;
        }
        QStatusBar {
            background: #11161c;
            color: #cdd6df;
        }
        """


def main() -> int:
    """Start the Qt application."""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
