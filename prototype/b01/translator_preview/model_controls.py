"""Native independent source/target and local model selectors."""
from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QWidget, QComboBox

from translator_core.languages import DEFAULT_DIRECTION, LANGUAGES, direction_pair
from translator_core.translation_routes import AUTO_MODEL
from .components import Button, Combo, hbox, label, vbox

MODEL_NAMES = {'hy-mt2:7b-q8':'HY-MT2 7B', 'hy-mt2:1.8b-q8':'HY-MT2 1.8B'}


class LanguagePair(QWidget):
    changed = Signal()

    def __init__(self):
        super().__init__()
        row=hbox(self,spacing=5)
        self.source=Combo()
        self.target=Combo()
        self.source.setAccessibleName('原文语言')
        self.target.setAccessibleName('译文语言')
        for key,(name,_,_) in LANGUAGES.items():
            self.source.addItem(name,key)
            self.target.addItem(name,key)
        row.addWidget(self.source)
        self.swap=Button('', 'swap', 'icon', self.exchange)
        self.swap.setAccessibleName('交换语言')
        self.swap.setToolTip('交换语言')
        self.swap.setFixedWidth(32)
        row.addWidget(self.swap)
        row.addWidget(self.target)
        self.set_direction(DEFAULT_DIRECTION)
        self.last=DEFAULT_DIRECTION
        self.source.currentIndexChanged.connect(lambda _:self.edit('source'))
        self.target.currentIndexChanged.connect(lambda _:self.edit('target'))

    def direction(self):
        return self.source.currentData()+'-'+self.target.currentData()

    def set_direction(self,direction):
        source,target=direction_pair(direction)
        with QSignalBlocker(self.source),QSignalBlocker(self.target):
            self.source.setCurrentIndex(self.source.findData(source))
            self.target.setCurrentIndex(self.target.findData(target))
        self.last=direction

    def edit(self, side):
        source,target=self.source.currentData(),self.target.currentData()
        if source==target:
            before,after=direction_pair(self.last)
            self.set_direction(source+'-'+before if side=='source' else after+'-'+target)
        self.last=self.direction()
        self.changed.emit()

    def exchange(self):
        source,target=direction_pair(self.direction())
        self.set_direction(target+'-'+source)
        self.changed.emit()


class TranslationModel(Combo):
    def __init__(self, client, caption='翻译模型'):
        super().__init__()
        self.client=client
        self.setAccessibleName(caption)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(10)
        self.setFixedWidth(222)
        self.set_model(AUTO_MODEL)
        client.model_catalog_changed.connect(self.reload)
        self.currentTextChanged.connect(self.setToolTip)

    def selected(self):
        return self.currentData() or AUTO_MODEL

    def effective(self):
        model=self.selected()
        return (self.client.model_catalog or [AUTO_MODEL])[0] if model==AUTO_MODEL else model

    def reload(self):
        self.set_model(self.selected())
        self.currentIndexChanged.emit(self.currentIndex())

    def set_model(self,model):
        models=self.client.model_catalog
        with QSignalBlocker(self):
            self.clear()
            first=models[0] if models else None
            automatic=MODEL_NAMES.get(first,first)+' · 自动' if first else '首个本机模型 · 自动'
            self.addItem(automatic,AUTO_MODEL)
            for name in models:
                self.addItem(MODEL_NAMES.get(name,name),name)
            if model!=AUTO_MODEL and self.findData(model)<0:
                self.addItem(MODEL_NAMES.get(model,model)+' · 未检测到',model)
            self.setCurrentIndex(self.findData(model))
        self.setToolTip(self.currentText())


def captioned(title,widget):
    box=QWidget()
    layout=vbox(box,spacing=4)
    layout.addWidget(label(title,'tiny'))
    layout.addWidget(widget)
    return box

