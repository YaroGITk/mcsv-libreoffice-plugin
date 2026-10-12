import uno
import unohelper
from com.sun.star.awt import XTopWindowListener

from . import core


def service(ctx, name):
    return ctx.ServiceManager.createInstanceWithContext(name, ctx)


def message(ctx, text, parent=None):
    toolkit = service(ctx, "com.sun.star.awt.Toolkit")
    box = toolkit.createMessageBox(
        parent or toolkit.getDesktopWindow(),
        uno.Enum("com.sun.star.awt.MessageBoxType", "ERRORBOX"),
        1,
        "MCSV",
        str(text),
    )
    try:
        box.execute()
    finally:
        box.dispose()


def add(model, kind, name, x, y, width, height, **props):
    control = model.createInstance("com.sun.star.awt.UnoControl" + kind + "Model")
    for key, value in dict(
        Name=name, PositionX=x, PositionY=y, Width=width, Height=height, **props
    ).items():
        setattr(control, key, value)
    model.insertByName(name, control)
    return control


class InitialFocus(unohelper.Base, XTopWindowListener):
    def __init__(self, dialog, field_name="delimiter"):
        self.dialog = dialog
        self.field_name = field_name
        self.done = False

    def windowActivated(self, event):
        if not self.done:
            self.done = True
            field = self.dialog.getControl(self.field_name)
            field.setFocus()
            selection = uno.createUnoStruct("com.sun.star.awt.Selection")
            selection.Min, selection.Max = 0, len(field.getText())
            field.setSelection(selection)

    def windowOpened(self, event):
        pass

    def windowClosing(self, event):
        pass

    def windowClosed(self, event):
        pass

    def windowMinimized(self, event):
        pass

    def windowNormalized(self, event):
        pass

    def windowDeactivated(self, event):
        pass

    def disposing(self, event):
        self.dialog = None


def center_dialog(dialog, toolkit, parent):
    area = toolkit.getWorkArea()
    if area.Width <= 0 or area.Height <= 0:
        if parent is None:
            return
        area = parent.getPosSize()
    bounds = dialog.getPosSize()
    dialog.setPosSize(
        area.X + max(0, (area.Width - bounds.Width) // 2),
        area.Y + max(0, (area.Height - bounds.Height) // 2),
        0,
        0,
        3,
    )


def workspace_settings(ctx, delimiter=";", parent=None):
    model = service(ctx, "com.sun.star.awt.UnoControlDialogModel")
    model.Title = "Создание набора CSV"
    model.Width, model.Height = 270, 130
    model.Closeable = True
    add(model, "FixedText", "label", 10, 10, 250, 14, Label="Разделитель новых листов")
    add(
        model,
        "ComboBox",
        "delimiter",
        10,
        28,
        250,
        16,
        StringItemList=(";", ",", "TAB", "|"),
        Dropdown=True,
        Text="TAB" if delimiter == "\t" else delimiter,
        Tabstop=True,
        TabIndex=0,
    )
    add(
        model,
        "FixedText",
        "note",
        10,
        52,
        250,
        42,
        Label=(
            "CSV сохраняет значения ячеек. Формулы превратятся в результаты, "
            "оформление не сохранится.\n"
            "Настройки можно изменить через «Файл → Свойства → Пользовательские свойства»."
        ),
        MultiLine=True,
    )
    add(
        model,
        "Button",
        "ok",
        138,
        105,
        58,
        16,
        Label="Создать",
        PushButtonType=1,
        DefaultButton=True,
    )
    add(model, "Button", "cancel", 200, 105, 58, 16, Label="Отмена", PushButtonType=2)
    dialog = service(ctx, "com.sun.star.awt.UnoControlDialog")
    dialog.setModel(model)
    dialog.setDesignMode(False)
    toolkit = service(ctx, "com.sun.star.awt.Toolkit")
    dialog.createPeer(toolkit, parent)
    try:
        center_dialog(dialog, toolkit, parent)
    except Exception:
        pass
    focus = InitialFocus(dialog)
    dialog.addTopWindowListener(focus)
    try:
        while dialog.execute() == 1:
            try:
                value = dialog.getControl("delimiter").getText()
                return core.validate_delimiter(
                    "\t" if value in ("TAB", "\\t") else value
                )
            except core.FormatError as exc:
                message(ctx, exc, dialog.getPeer())
        return None
    finally:
        dialog.removeTopWindowListener(focus)
        dialog.dispose()
