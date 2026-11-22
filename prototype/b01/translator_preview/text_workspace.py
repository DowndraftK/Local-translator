"""Unified text view. TextFlow coordinates the existing worker."""
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QWidget, QStackedWidget, QApplication, QFileDialog

from translator_core.contracts import default_recipe
from translator_core.store import atomic_write
from translator_core.text_policy import analyze_concise_text
from translator_core.dictionary.rules import analyze_query
from .components import Button, Combo, IconLabel, ReadingEdit, frame, hbox, label, scrollable, vbox
from .workspace import PageLifecycle, ResultOwner, ResultPresentation


@dataclass(frozen=True)
class TextDraft:
    text: str
    position: int
    anchor: int
    direction_index: int
    configuration: dict
    effective_revision: str
    owner: ResultOwner
    result: object


class TextWorkspace(QWidget, PageLifecycle):
    draft_changed = Signal()
    translate_requested = Signal()
    export_requested = Signal()
    lookup_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.presentation = ResultPresentation()
        self.configuration = default_recipe("en-zh")
        self.effective_revision = "draft"
        self.restoring = False
        self.flow = None
        self.analysis = None
        self._analysis_input = None
        box = vbox(self, (36, 28, 36, 20), 18)
        heading = hbox()
        heading.addWidget(label("文字翻译", "heading")); heading.addStretch()
        self.new_button = Button("新草稿", "file", "quiet")
        self.edit_button = Button("编辑为新草稿", None, "quiet")
        self.tools = Combo(); self.tools.setAccessibleName("文字任务操作")
        self.tools.addItems(["任务操作…", "仅保存原文", "保存当前状态", "检查本机翻译服务", "导入旧任务副本…", "重新打开当前任务", "导出完整原文与状态…"])
        self.tools.setFixedWidth(190)
        for widget in (self.new_button, self.edit_button, self.tools): heading.addWidget(widget)
        box.addLayout(heading)
        card = frame("editor_card"); content = vbox(card)
        bar = frame("languagebar"); row = hbox(bar, (20, 10, 20, 10), 10)
        self.direction = Combo(); self.direction.addItem("英语", "en-zh"); self.direction.addItem("简体中文", "zh-en")
        self.direction.setAccessibleName("原文语言"); self.direction.setFixedWidth(124)
        row.addWidget(self.direction)
        self.swap = Button("", "swap", "icon", lambda: self.direction.setCurrentIndex(1-self.direction.currentIndex()))
        self.swap.setAccessibleName("交换语言"); self.swap.setFixedWidth(36); row.addWidget(self.swap)
        self.target_language = Combo(); self.target_language.addItems(["简体中文", "英语"])
        self.target_language.setAccessibleName("译文语言"); self.target_language.setFixedWidth(124); row.addWidget(self.target_language)
        row.addStretch()
        content.addWidget(bar)
        panels = hbox()
        left = QWidget(); source_box = vbox(left, (24, 20, 24, 16), 12)
        head = hbox(); head.addWidget(label("原文", "panel_title")); head.addStretch()
        self.clear_button = Button("", "close", "icon", self.clear)
        self.clear_button.setAccessibleName("清空原文"); self.clear_button.setFixedWidth(28); head.addWidget(self.clear_button)
        source_box.addLayout(head)
        self.source = ReadingEdit(); self.source.setObjectName("sourceEditor")
        self.source.setAccessibleName("原文输入框"); self.source.setPlaceholderText("输入或粘贴需要翻译的内容…")
        source_box.addWidget(self.source, 1)
        actions = hbox(spacing=8)
        self.paste_button = Button("粘贴", "paste", "quiet", lambda: self.source.insertPlainText(QApplication.clipboard().text()))
        actions.addWidget(self.paste_button)
        self.source_lookup = Button("查词", "search", "quiet", lambda:self.lookup_selection(self.source))
        self.source_lookup.setAccessibleName("原文选区查词"); actions.addWidget(self.source_lookup)
        self.return_button = Button("返回草稿", "back", "quiet"); self.return_button.hide(); actions.addWidget(self.return_button)
        actions.addStretch(); self.char_count = label("0 字符", "tiny"); actions.addWidget(self.char_count)
        source_box.addLayout(actions); panels.addWidget(left, 1)
        line = frame("vertical_divider"); line.setFixedWidth(1); panels.addWidget(line)
        right = QWidget(); target_box = vbox(right, (24, 20, 24, 16), 12)
        target_box.addWidget(label("译文", "panel_title"))
        self.result_scope = label("", "tiny", True); self.result_scope.hide(); target_box.addWidget(self.result_scope)
        self.output_stack = QStackedWidget()
        empty = QWidget(); e = vbox(empty, spacing=14); e.addStretch()
        e.addWidget(IconLabel("translate", 48, "muted"), 0, Qt.AlignCenter)
        hint = label("译文将在这里显示", "tiny"); hint.setAlignment(Qt.AlignCenter); e.addWidget(hint); e.addStretch()
        self.output_stack.addWidget(empty)
        self.output = ReadingEdit(); self.output.setReadOnly(True); self.output.setObjectName("targetEditor")
        self.output.setAccessibleName("主要译文"); self.output_stack.addWidget(self.output)
        self.detail_body = QWidget(); self.detail_layout = vbox(self.detail_body, (0, 0, 10, 10), 18)
        self.detail_scroll = scrollable(self.detail_body); self.detail_scroll.setAccessibleName("翻译详解")
        self.output_stack.addWidget(self.detail_scroll); target_box.addWidget(self.output_stack, 1)
        actions = hbox(spacing=6)
        self.copy_button = Button("复制", "copy", "quiet", self.copy)
        self.export_button = Button("导出", "download", "quiet", self.export)
        actions.addWidget(self.copy_button); actions.addWidget(self.export_button); actions.addStretch()
        self.output_lookup = Button("查词", "search", "quiet", lambda:self.lookup_selection(self.output))
        self.output_lookup.setAccessibleName("主译文选区查词"); actions.addWidget(self.output_lookup)
        self.favorite = Button("", "bookmark", "icon"); self.favorite.setEnabled(False)
        self.favorite.setToolTip("保存任务后可收藏"); self.favorite.setAccessibleName("收藏当前文字"); self.favorite.setFixedWidth(34)
        actions.addWidget(self.favorite); target_box.addLayout(actions); panels.addWidget(right, 1)
        content.addLayout(panels, 1); box.addWidget(card, 1)
        bottom = hbox(spacing=10)
        self.notice = label("输入原文后点击翻译 · ⌘ / Ctrl + Enter", "tiny", True)
        bottom.addWidget(self.notice, 1)
        self.stop_button = Button("停止", None, "quiet"); self.stop_button.hide(); bottom.addWidget(self.stop_button)
        self.translate_button = Button("翻译", "arrow", "primary", self.translate_requested.emit)
        self.translate_button.setFixedHeight(44); bottom.addWidget(self.translate_button); box.addLayout(bottom)
        self.source.textChanged.connect(self.source_changed)
        self.direction.currentIndexChanged.connect(self.direction_changed)
        self.target_language.currentIndexChanged.connect(self.direction.setCurrentIndex)
        self.source.cursorPositionChanged.connect(lambda: self.draft_changed.emit() if not self.restoring else None)
        self.source.selectionChanged.connect(lambda: self.draft_changed.emit() if not self.restoring else None)
        self.source_changed()

    def owner(self, **identity):
        return ResultOwner.for_input(self.source.toPlainText(), self.direction.currentData(), self.configuration,
                                     self.effective_revision, **identity)

    def source_changed(self):
        if not self.restoring:
            self.presentation.bind(self.owner())
            # Editing, including an edit then undo, invalidates the previous display.
            self.presentation.clear()
            self.draft_changed.emit()
        self.render()

    def direction_changed(self, index):
        self.target_language.blockSignals(True); self.target_language.setCurrentIndex(index); self.target_language.blockSignals(False)
        if not self.restoring:
            self.configuration = default_recipe(self.direction.currentData())
            self.source_changed()

    def set_configuration(self, configuration, effective_revision):
        self.configuration, self.effective_revision = configuration, effective_revision
        self.presentation.bind(self.owner())
        self.render()

    def expect_result(self, owner):
        # Explicitly bind a future task/request/generation before accepting its result.
        if (owner.input_sha256, owner.direction, owner.configuration_sha256, owner.effective_revision) != (
                self.owner().input_sha256, self.owner().direction, self.owner().configuration_sha256, self.owner().effective_revision):
            return False
        self.presentation.bind(owner)
        return True

    def present_result(self, result):
        accepted = self.presentation.accept(result)
        if accepted:
            self.render()
        return accepted

    def clear(self):
        if self.source.isReadOnly(): return
        self.source.clear()
        self.presentation.clear(); self.render()

    def capture_draft(self):
        cursor = self.source.textCursor()
        return TextDraft(self.source.toPlainText(), cursor.position(), cursor.anchor(), self.direction.currentIndex(),
                         self.configuration, self.effective_revision, self.presentation.owner, self.presentation.result)

    def restore_draft(self, draft):
        self.restoring = True
        self.direction.setCurrentIndex(draft.direction_index)
        self.configuration, self.effective_revision = draft.configuration, draft.effective_revision
        self.source.setPlainText(draft.text)
        cursor = self.source.textCursor(); cursor.setPosition(draft.anchor)
        cursor.setPosition(draft.position, QTextCursor.KeepAnchor); self.source.setTextCursor(cursor)
        self.presentation.bind(draft.owner)
        self.presentation.result = draft.result
        self.restoring = False; self.render()

    def render(self):
        result = self.presentation.result
        key = (self.source.toPlainText(), self.direction.currentData())
        if key != self._analysis_input:
            self.analysis = analyze_concise_text(*key)
            self._analysis_input = key
        count = self.analysis
        self.char_count.setText(f"{count.graphemes:,} 字符" +
                               (f" · {count.english_words:,} 词" if count.english_words is not None else ""))
        self.clear_button.setEnabled(bool(self.source.toPlainText()))
        self.translate_button.setEnabled(not count.is_empty)
        for button in (self.copy_button, self.export_button): button.setEnabled(result is not None)
        text = result.translation if result else ""
        if text != self.output.toPlainText():
            scroll = self.output.verticalScrollBar(); position = scroll.value()
            self.output.setPlainText(text); scroll.setValue(position)
        if self.flow: self.flow.update_controls()
        if not result:
            self.output_stack.setCurrentIndex(0); return
        if result.explanation is None:
            self.output_stack.setCurrentIndex(1); return
        while self.detail_layout.count():
            item = self.detail_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        def add(text, kind="detail_note"):
            widget = label(text, kind, True)
            widget.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            self.detail_layout.addWidget(widget)
        add(result.translation, "detail_translation")
        detail = result.explanation
        add("其他译法", "detail_heading")
        for item in detail.alternatives:
            add(item.text, "detail_term"); add(item.context); add(item.explanation)
        add("用法说明", "detail_heading")
        for note in detail.usage_notes: add(note)
        add("双语例句", "detail_heading")
        for pair in detail.examples: add(pair.source, "detail_example"); add(pair.translation)
        self.detail_layout.addStretch(); self.output_stack.setCurrentIndex(2)

    def lookup_selection(self, editor):
        if editor is self.output:
            result = self.presentation.result
            if (not result or result.owner != self.presentation.owner or
                    result.owner != self.owner(task_id=result.owner.task_id,
                        task_revision=result.owner.task_revision, request_id=result.owner.request_id,
                        generation=result.owner.generation) or
                    editor.toPlainText() != result.translation.replace("\r\n", "\n").replace("\u2029", "\n")):
                self.notice.setText("请选择当前有效主译文中的词语。"); return False
        cursor = editor.textCursor()
        # selectedText uses Qt's UTF-16 cursor; Python slicing that offset would
        # split supplementary characters. Paragraph separators become LF only.
        raw = cursor.selectedText().replace("\u2029", "\n")
        analysis = analyze_query(raw)
        if not cursor.hasSelection() or not analysis.directions:
            self.notice.setText("请选择中文或英文词语（最多128字素且2048 UTF-8字节），不会自动截取选区。")
            return False
        self.lookup_requested.emit(raw); return True

    def copy(self):
        if self.presentation.result: QApplication.clipboard().setText(self.presentation.result.translation)

    def export(self):
        if not self.presentation.result: return
        if self.flow:
            self.export_requested.emit(); return
        path, _ = QFileDialog.getSaveFileName(self, "导出双语 TXT", "文字翻译-双语.txt", "UTF-8文字 (*.txt)")
        if path:
            try:
                atomic_write((self.source.toPlainText()+"\n\n"+self.presentation.result.translation+"\n").encode(), Path(path))
            except (OSError, ValueError) as exc:
                self.notice.setText("导出未完成："+str(exc))
