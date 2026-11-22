/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2017-2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "RDStyle.h"
#include <QAbstractItemView>
#include <QApplication>
#include <QComboBox>
#include <QCommonStyle>
#include <QDebug>
#include <QPainter>
#include <QPainterPath>
#include <QPen>
#include <QStyleOption>
#include <QtMath>
#include "Code/QRDUtils.h"
#include "Widgets/Extended/RDTreeView.h"

#if defined(Q_OS_WIN32)
#include <qt_windows.h>
#endif

namespace Constants
{
static const int SliderHandleHalfWidth = 5;
static const int SliderGrooveHeight = 4;
static const qreal SliderHandleCornerRadius = 4.0f;

static const int ButtonMargin = 6;
static const int ButtonBorder = 1;

static const int HighlightBorder = 2;

static const int CheckWidth = 14;
static const int CheckHeight = 14;
static const float CheckCornerSize = 2.0f;
static const int CheckMargin = 3;

static const int GroupHMargin = 8;
static const int GroupVMargin = 4;

static const int ScrollButtonDim = 12;
static const int ScrollBarMargin = 2;
static const int ScrollBarMin = ScrollButtonDim;
static const qreal ScrollBarRadius = 4.0;

static const int SeparatorMargin = 2;

static const int ComboMargin = 2;
static const int ComboArrowDim = 12;

static const int SpinButtonDim = 12;
static const int SpinMargin = 1;

static const int ProgressMargin = 2;
static const qreal ProgressRadius = 4.0;

static const int MenuBarMargin = 6;
static const int MenuSubmenuWidth = 8;
static const int MenuBarIconSize = 16;
static const int MenuBarMinimumWidth = 80;

static const int TabWidgetBorder = 1;
static const int TabMargin = 4;
static const int TabMinWidth = 75;
static const int TabMaxWidth = 250;

static const int ItemHeaderMargin = 4;
static const int ItemHeaderIconSize = 16;
};

namespace UltraColors
{
static const QRgb AppToolbarBackground = qRgb(25, 25, 25);
static const QRgb DefaultBackground = qRgb(40, 40, 40);
static const QRgb InputBackground = qRgb(42, 42, 42);
static const QRgb WindowBackground = qRgb(56, 56, 56);
static const QRgb ToolbarBackground = qRgb(60, 60, 60);
static const QRgb AlternateBackground = qRgb(63, 63, 63);

static const QRgb TabBackground = qRgb(40, 40, 40);
static const QRgb TabBackgroundSelected = qRgb(40, 40, 40);
static const QRgb TabBackgroundHover = qRgb(48, 48, 48);
static const QRgb TabText = qRgb(189, 189, 189);

static const QRgb ButtonBackground = qRgb(88, 88, 88);
static const QRgb ButtonBackgroundHover = qRgb(103, 103, 103);
static const QRgb ButtonBackgroundFocus = qRgb(110, 110, 110);
static const QRgb ButtonBackgroundPressed = qRgb(70, 96, 124);
static const QRgb ButtonBackgroundHoverPressed = qRgb(79, 101, 127);
static const QRgb ButtonDisabled = qRgb(75, 75, 75);

static const QRgb DropdownBackground = qRgb(81, 81, 81);
static const QRgb DropdownBackgroundHover = qRgb(88, 88, 88);

static const QRgb DefaultBorder = qRgb(35, 35, 35);
static const QRgb WindowBorder = qRgb(36, 36, 36);
static const QRgb ButtonBorder = qRgb(48, 48, 48);
static const QRgb AccentBorder = qRgb(36, 36, 36);
static const QRgb InputBorder = qRgb(33, 33, 33);
static const QRgb DeepBorder = qRgb(13, 13, 13);
static const QRgb HoverBorder = qRgb(101, 101, 101);
static const QRgb FocusBorder = qRgb(58, 121, 187);
static const QRgb ButtonFocusBorder = qRgb(123, 174, 250);

static const QRgb DefaultText = qRgb(210, 210, 210);
static const QRgb LabelText = qRgb(196, 196, 196);
static const QRgb ButtonText = qRgb(238, 238, 238);
static const QRgb DropdownText = qRgb(228, 228, 228);
static const QRgb DisabledText = qRgb(122, 122, 122);
static const QRgb DisabledButtonText = qRgb(163, 163, 163);

static const QRgb Highlight = qRgb(44, 93, 135);
static const QRgb InactiveHighlight = qRgb(77, 77, 77);
static const QRgb Link = qRgb(76, 126, 255);
static const QRgb LinkVisited = qRgb(255, 0, 255);

static const QRgb HeaderBackgroundHover = qRgb(70, 70, 70);
static const QRgb HeaderBackgroundPressed = qRgb(80, 80, 80);
static const QRgb DockTitleBackground = qRgb(40, 40, 40);
static const QRgb AppToolbarBackgroundHover = qRgb(66, 66, 66);
static const QRgb AppToolbarBackgroundPressed = qRgb(106, 106, 106);

static const QRgb ScrollbarThumb = qRgb(95, 95, 95);
static const QRgb ScrollbarThumbHover = qRgb(104, 104, 104);
static const QRgb ScrollbarThumbBorder = qRgb(50, 50, 50);
static const QRgb SliderGroove = qRgb(94, 94, 94);
static const QRgb SliderThumb = qRgb(153, 153, 153);
static const QRgb SliderThumbHover = qRgb(234, 234, 234);
static const QRgb SliderThumbDisabled = qRgb(102, 102, 102);

static const QRgb ToolTipBackground = qRgb(55, 55, 55);

static QColor color(QRgb rgb)
{
  return QColor::fromRgb(rgb);
}
};

#if defined(Q_OS_WIN32)
namespace
{
typedef HRESULT(WINAPI *DwmSetWindowAttributePtr)(HWND, DWORD, LPCVOID, DWORD);

bool hasNativeTitleBar(const QWidget *widget)
{
  if(!widget || !widget->isWindow())
    return false;

  const Qt::WindowType type = widget->windowType();
  return type == Qt::Window || type == Qt::Dialog || type == Qt::Tool;
}

// Qt 5.9 predates native dark-title-bar support. Apply Ultra's title colour through DWM while
// retaining Windows' native move, resize, snap and caption-button behaviour. Numbered attributes
// keep this source compatible with the older Windows SDK used by RenderDoc.
void updateNativeTitleBar(QWidget *window, bool ultra)
{
  static DwmSetWindowAttributePtr setWindowAttribute = []() -> DwmSetWindowAttributePtr {
    // Retain this system module for the process lifetime so the cached procedure stays valid.
    HMODULE dwmapi = LoadLibraryW(L"dwmapi.dll");

    return dwmapi ? reinterpret_cast<DwmSetWindowAttributePtr>(
                        GetProcAddress(dwmapi, "DwmSetWindowAttribute"))
                  : NULL;
  }();

  if(!setWindowAttribute || !window->testAttribute(Qt::WA_WState_Created))
    return;

  HWND hwnd = reinterpret_cast<HWND>(window->winId());
  const BOOL useDarkMode = ultra ? TRUE : FALSE;

  // 20 is supported by current Windows 10/11 builds; 19 is the older Windows 10 spelling.
  HRESULT result = setWindowAttribute(hwnd, 20, &useDarkMode, sizeof(useDarkMode));
  if(FAILED(result))
    setWindowAttribute(hwnd, 19, &useDarkMode, sizeof(useDarkMode));

  // Windows 11 supports exact border/caption/text colours. Unsupported attributes fail safely,
  // leaving the immersive dark-mode title bar above in place on Windows 10.
  const DWORD defaultColor = 0xffffffff;
  if(ultra)
  {
    const QColor background = UltraColors::color(UltraColors::DefaultBackground);
    const QColor foreground = UltraColors::color(UltraColors::TabText);
    const QColor border = UltraColors::color(UltraColors::WindowBorder);
    const COLORREF backgroundColor =
        RGB(background.red(), background.green(), background.blue());
    const COLORREF textColor = RGB(foreground.red(), foreground.green(), foreground.blue());
    const COLORREF borderColor = RGB(border.red(), border.green(), border.blue());

    setWindowAttribute(hwnd, 34, &borderColor, sizeof(borderColor));
    setWindowAttribute(hwnd, 35, &backgroundColor, sizeof(backgroundColor));
    setWindowAttribute(hwnd, 36, &textColor, sizeof(textColor));
  }
  else
  {
    setWindowAttribute(hwnd, 34, &defaultColor, sizeof(defaultColor));
    setWindowAttribute(hwnd, 35, &defaultColor, sizeof(defaultColor));
    setWindowAttribute(hwnd, 36, &defaultColor, sizeof(defaultColor));
  }
}
}
#endif

namespace Animation
{
QHash<QObject *, QAbstractAnimation *> animations;

bool has(QObject *target)
{
  return animations.contains(target);
}

QAbstractAnimation *get(QObject *target)
{
  return animations.value(target);
}

template <typename AnimationType>
AnimationType *get(QObject *target)
{
  return (AnimationType *)get(target);
}

void stop(QObject *target)
{
  if(has(target))
  {
    QAbstractAnimation *existing = get(target);
    existing->stop();
    delete existing;
    animations.remove(target);
  }
}

void removeOnDelete(QObject *target)
{
  if(has(target))
    animations.remove(target);
}

void start(QAbstractAnimation *anim)
{
  QObject *target = anim->parent();

  stop(target);
  if(has(target))
  {
    QAbstractAnimation *existing = get(target);
    existing->stop();
    delete existing;
    animations.remove(target);
  }

  animations.insert(target, anim);
  QObject::connect(target, &QObject::destroyed, &removeOnDelete);
  anim->start();
}
};

static QWindow *widgetWindow(const QWidget *widget)
{
  return widget ? widget->window()->windowHandle() : NULL;
}

RDStyle::RDStyle(ColorScheme scheme) : RDTweakedNativeStyle(new QCommonStyle())
{
  m_Scheme = scheme;

  const uchar bits[] = {
      0x19,    // X..XX
      0x1C,    // ..XXX
      0x0E,    // .XXX.
      0x07,    // XXX..
      0x13,    // XX..X
  };

  m_PartialCheckPattern = QBitmap::fromData(QSize(5, 5), bits);
}

RDStyle::~RDStyle()
{
}

void RDStyle::polishPalette(QPalette &pal) const
{
  int h = 0, s = 0, v = 0;

  QColor windowText;
  QColor window;
  QColor base;
  QColor highlight;
  QColor tooltip;

  if(m_Scheme == Light)
  {
    window = QColor(225, 225, 225);
    windowText = QColor(Qt::black);
    base = QColor(Qt::white);
    highlight = QColor(80, 110, 160);
    tooltip = QColor(250, 245, 200);
  }
  else if(m_Scheme == Dark)
  {
    window = QColor(45, 55, 60);
    windowText = QColor(225, 225, 225);
    base = QColor(22, 27, 30);
    highlight = QColor(100, 130, 200);
    tooltip = QColor(70, 70, 65);
  }
  else
  {
    window = UltraColors::color(UltraColors::WindowBackground);
    windowText = UltraColors::color(UltraColors::LabelText);
    base = UltraColors::color(UltraColors::DefaultBackground);
    highlight = UltraColors::color(UltraColors::Highlight);
    tooltip = UltraColors::color(UltraColors::ToolTipBackground);
  }

  QColor light = window.lighter(150);
  QColor mid = window.darker(150);
  QColor dark = mid.darker(150);

  QColor text = windowText;

  pal = QPalette(windowText, window, light, dark, mid, text, base);

  pal.setColor(QPalette::Shadow, Qt::black);

  if(m_Scheme == Ultra)
  {
    pal.setColor(QPalette::WindowText, UltraColors::color(UltraColors::LabelText));
    pal.setColor(QPalette::Button, UltraColors::color(UltraColors::ButtonBackground));
    pal.setColor(QPalette::Light, UltraColors::color(UltraColors::ButtonBorder));
    pal.setColor(QPalette::Midlight, UltraColors::color(UltraColors::HeaderBackgroundHover));
    pal.setColor(QPalette::Dark, UltraColors::color(UltraColors::AccentBorder));
    pal.setColor(QPalette::Mid, UltraColors::color(UltraColors::ButtonBorder));
    pal.setColor(QPalette::Text, UltraColors::color(UltraColors::DefaultText));
    pal.setColor(QPalette::BrightText, UltraColors::color(UltraColors::ButtonText));
    pal.setColor(QPalette::ButtonText, UltraColors::color(UltraColors::ButtonText));
    pal.setColor(QPalette::Base, UltraColors::color(UltraColors::DefaultBackground));
    pal.setColor(QPalette::AlternateBase, UltraColors::color(UltraColors::AlternateBackground));
    pal.setColor(QPalette::Window, UltraColors::color(UltraColors::WindowBackground));
    pal.setColor(QPalette::Shadow, UltraColors::color(UltraColors::AppToolbarBackground));
    pal.setColor(QPalette::ToolTipBase, UltraColors::color(UltraColors::ToolTipBackground));
    pal.setColor(QPalette::ToolTipText, UltraColors::color(UltraColors::DefaultText));
    pal.setColor(QPalette::Highlight, UltraColors::color(UltraColors::Highlight));
    pal.setColor(QPalette::HighlightedText, Qt::white);
    pal.setColor(QPalette::Link, UltraColors::color(UltraColors::Link));
    pal.setColor(QPalette::LinkVisited, UltraColors::color(UltraColors::LinkVisited));

    pal.setColor(QPalette::Inactive, QPalette::Highlight,
                 UltraColors::color(UltraColors::InactiveHighlight));
    pal.setColor(QPalette::Inactive, QPalette::HighlightedText, Qt::white);

    pal.setColor(QPalette::Disabled, QPalette::WindowText,
                 UltraColors::color(UltraColors::DisabledText));
    pal.setColor(QPalette::Disabled, QPalette::Text,
                 UltraColors::color(UltraColors::DisabledText));
    pal.setColor(QPalette::Disabled, QPalette::ButtonText,
                 UltraColors::color(UltraColors::DisabledButtonText));
    pal.setColor(QPalette::Disabled, QPalette::BrightText,
                 UltraColors::color(UltraColors::DisabledButtonText));
    pal.setColor(QPalette::Disabled, QPalette::Button,
                 UltraColors::color(UltraColors::ButtonDisabled));
    pal.setColor(QPalette::Disabled, QPalette::Base,
                 UltraColors::color(UltraColors::DefaultBackground));
    pal.setColor(QPalette::Disabled, QPalette::Window,
                 UltraColors::color(UltraColors::WindowBackground));
    pal.setColor(QPalette::Disabled, QPalette::Highlight,
                 UltraColors::color(UltraColors::InactiveHighlight));
    pal.setColor(QPalette::Disabled, QPalette::HighlightedText,
                 UltraColors::color(UltraColors::DisabledButtonText));
    pal.setColor(QPalette::Disabled, QPalette::Link,
                 UltraColors::color(UltraColors::DisabledText));
    pal.setColor(QPalette::Disabled, QPalette::LinkVisited,
                 UltraColors::color(UltraColors::DisabledText));
    return;
  }

  if(m_Scheme == Light)
    pal.setColor(QPalette::AlternateBase, base.darker(110));
  else
    pal.setColor(QPalette::AlternateBase, base.lighter(110));

  if(m_Scheme != Light)
  {
    pal.setColor(QPalette::BrightText, text);
  }

  pal.setColor(QPalette::ToolTipBase, tooltip);
  pal.setColor(QPalette::ToolTipText, text);

  pal.setColor(QPalette::Highlight, highlight);
  // inactive highlight is desaturated
  highlight.getHsv(&h, &s, &v);
  highlight.setHsv(h, int(s * 0.5), v);
  pal.setColor(QPalette::Inactive, QPalette::Highlight, highlight);

  pal.setColor(QPalette::HighlightedText, Qt::white);

  // links are based on the highlight colour
  QColor link = m_Scheme == Light ? highlight.darker(125) : highlight.lighter(105);
  pal.setColor(QPalette::Link, link);

  // visited links are desaturated
  QColor linkVisited = link;
  linkVisited.getHsv(&h, &s, &v);
  linkVisited.setHsv(h, 0, v);
  pal.setColor(QPalette::LinkVisited, linkVisited);

  // for the 'text' type roles, make the disabled colour half as bright
  for(QPalette::ColorRole role :
      {QPalette::WindowText, QPalette::Text, QPalette::ButtonText, QPalette::Highlight,
       QPalette::HighlightedText, QPalette::Link, QPalette::LinkVisited})
  {
    QColor col = pal.color(QPalette::Inactive, role);

    col.getHsv(&h, &s, &v);

    // with the exception of link text, the disabled version is desaturated
    if(role != QPalette::Link)
      s = 0;

    // black is the only colour that gets brighter, any other colour gets darker
    if(s == 0 && v == 0)
    {
      pal.setColor(QPalette::Disabled, role, QColor(160, 160, 160));
    }
    else
    {
      col.setHsv(h, s, v / 2);
      pal.setColor(QPalette::Disabled, role, col);
    }
  }

  // the 'base' roles get every so slightly darker, but not as much as text
  for(QPalette::ColorRole role : {QPalette::Base, QPalette::Window, QPalette::Button})
  {
    QColor col = pal.color(QPalette::Inactive, role);

    col.getHsv(&h, &s, &v);
    col.setHsv(h, s, v * 0.9);
    pal.setColor(QPalette::Disabled, role, col);
  }
}

void RDStyle::polish(QWidget *widget)
{
  QTabBar *tabbar = qobject_cast<QTabBar *>(widget);
  if(qobject_cast<QAbstractSlider *>(widget) || tabbar ||
     qobject_cast<QAbstractItemView *>(widget))
    widget->setAttribute(Qt::WA_Hover);

  if(tabbar && m_Scheme == Ultra)
  {
    if(!tabbar->property("_renderdoc_ultra_previous_palette").isValid())
    {
      tabbar->setProperty("_renderdoc_ultra_previous_palette", tabbar->palette());
      tabbar->setProperty("_renderdoc_ultra_previous_autofill", tabbar->autoFillBackground());
    }

    QPalette tabPalette = tabbar->palette();
    const QColor background = UltraColors::color(UltraColors::DefaultBackground);
    for(QPalette::ColorGroup group :
        {QPalette::Active, QPalette::Inactive, QPalette::Disabled})
    {
      tabPalette.setColor(group, QPalette::Window, background);
      tabPalette.setColor(group, QPalette::Base, background);
    }
    tabbar->setPalette(tabPalette);
    tabbar->setAutoFillBackground(true);
  }

#if defined(Q_OS_WIN32)
  if(m_Scheme == Ultra && hasNativeTitleBar(widget))
  {
    widget->installEventFilter(this);
    if(widget->isVisible())
      updateNativeTitleBar(widget, true);
  }
#endif

  RDTreeView *tree = qobject_cast<RDTreeView *>(widget);
  if(tree && m_Scheme == Ultra && tree->verticalItemMargin() == 6)
  {
    tree->setItemVerticalMargin(2);
    tree->setProperty("_renderdoc_ultra_compact_rows", true);
  }

  QTabWidget *tabwidget = qobject_cast<QTabWidget *>(widget);
  if(tabwidget && tabwidget->inherits("ToolWindowManagerArea"))
  {
    tabwidget->installEventFilter(this);
    tabwidget->setDocumentMode(false);
    tabwidget->tabBar()->setDrawBase(true);
  }
}

void RDStyle::polish(QApplication *app)
{
  app->setPalette(standardPalette());
}

void RDStyle::unpolish(QWidget *widget)
{
  Animation::stop(widget);

  QTabBar *tabbar = qobject_cast<QTabBar *>(widget);
  if(tabbar && tabbar->property("_renderdoc_ultra_previous_palette").isValid())
  {
    tabbar->setPalette(
        tabbar->property("_renderdoc_ultra_previous_palette").value<QPalette>());
    tabbar->setAutoFillBackground(
        tabbar->property("_renderdoc_ultra_previous_autofill").toBool());
    tabbar->setProperty("_renderdoc_ultra_previous_palette", QVariant());
    tabbar->setProperty("_renderdoc_ultra_previous_autofill", QVariant());
  }

#if defined(Q_OS_WIN32)
  if(hasNativeTitleBar(widget))
  {
    if(m_Scheme == Ultra)
      updateNativeTitleBar(widget, false);
    widget->removeEventFilter(this);
  }
#endif

  RDTreeView *tree = qobject_cast<RDTreeView *>(widget);
  if(tree && tree->property("_renderdoc_ultra_compact_rows").toBool())
  {
    tree->setItemVerticalMargin(6);
    tree->setProperty("_renderdoc_ultra_compact_rows", QVariant());
  }

  QTabWidget *tabwidget = qobject_cast<QTabWidget *>(widget);
  if(tabwidget && tabwidget->inherits("ToolWindowManagerArea"))
    tabwidget->removeEventFilter(this);
}

QPalette RDStyle::standardPalette() const
{
  QPalette ret = RDTweakedNativeStyle::standardPalette();

  polishPalette(ret);

  return ret;
}

bool RDStyle::eventFilter(QObject *watched, QEvent *event)
{
#if defined(Q_OS_WIN32)
  QWidget *window = qobject_cast<QWidget *>(watched);
  if(m_Scheme == Ultra && hasNativeTitleBar(window) &&
     (event->type() == QEvent::Show || event->type() == QEvent::PaletteChange ||
      event->type() == QEvent::StyleChange || event->type() == QEvent::WinIdChange))
  {
    updateNativeTitleBar(window, true);
  }
#endif

  QTabWidget *tabwidget = qobject_cast<QTabWidget *>(watched);
  if(tabwidget && tabwidget->inherits("ToolWindowManagerArea"))
  {
    if(tabwidget->documentMode())
      tabwidget->setDocumentMode(false);
    if(!tabwidget->tabBar()->drawBase())
      tabwidget->tabBar()->setDrawBase(true);
  }

  return QObject::eventFilter(watched, event);
}

QRect RDStyle::subControlRect(ComplexControl cc, const QStyleOptionComplex *opt, SubControl sc,
                              const QWidget *widget) const
{
  if(cc == QStyle::CC_GroupBox)
  {
    QRect ret = opt->rect;

    if(sc == SC_GroupBoxFrame)
      return ret;

    const QStyleOptionGroupBox *group = qstyleoption_cast<const QStyleOptionGroupBox *>(opt);

    const int border = Constants::ButtonBorder;
    const int lineHeight = group->fontMetrics.height();

    ret.adjust(border, border, -2 * border, -2 * border);

    const int labelHeight = lineHeight + border * 2;

    const int checkWidth =
        (group->subControls & QStyle::SC_GroupBoxCheckBox) ? Constants::CheckWidth : 0;

    if(sc == SC_GroupBoxLabel)
    {
      if(checkWidth > 0)
      {
        ret.adjust(checkWidth + Constants::CheckMargin, 0, 0, 0);
        ret.setHeight(qMax(labelHeight, Constants::CheckHeight));
      }
      else
      {
        ret.setHeight(labelHeight);
      }

      ret.setWidth(group->fontMetrics.boundingRect(group->text).width());

      return ret;
    }

    if(sc == SC_GroupBoxCheckBox)
    {
      if(checkWidth > 0)
      {
        ret.setWidth(checkWidth);
        ret.setHeight(Constants::CheckHeight);
        ret.adjust(Constants::CheckMargin, Constants::CheckMargin, Constants::CheckMargin,
                   Constants::CheckMargin);
      }
      else
      {
        ret = QRect();
      }
      return ret;
    }

    if(sc == QStyle::SC_GroupBoxContents)
    {
      ret.setTop(ret.top() + labelHeight + Constants::GroupHMargin);

      return ret;
    }

    return opt->rect;
  }
  else if(cc == QStyle::CC_ScrollBar)
  {
    QRect ret = opt->rect;

    // shrink by the border
    ret.adjust(1, 1, -1, -1);

    // don't have first/last buttons
    if(sc == QStyle::SC_ScrollBarFirst || sc == QStyle::SC_ScrollBarLast)
      return QRect();

    const QStyleOptionSlider *scroll = qstyleoption_cast<const QStyleOptionSlider *>(opt);
    const int range = scroll->maximum - scroll->minimum;

    if(scroll->orientation == Qt::Horizontal)
    {
      if(sc == QStyle::SC_ScrollBarSubLine)
        return ret.adjusted(0, 0, -ret.width() + Constants::ScrollButtonDim, 0);
      if(sc == QStyle::SC_ScrollBarAddLine)
        return ret.adjusted(ret.width() - Constants::ScrollButtonDim, 0, 0, 0);

      const int buttonAdjust = Constants::ScrollButtonDim + Constants::ScrollBarMargin;
      ret.adjust(buttonAdjust, 0, -buttonAdjust, 0);

      if(sc == QStyle::SC_ScrollBarGroove)
        return ret;

      QRect slider = ret;

      if(scroll->maximum > scroll->minimum)
      {
        int sliderSize = qMax(Constants::ScrollBarMin,
                              (scroll->pageStep * ret.width()) / (range + scroll->pageStep));

        slider.setWidth(qMin(slider.width(), sliderSize));
        slider.moveLeft(ret.left() + (qreal(scroll->sliderPosition) / qreal(range)) *
                                         (ret.width() - slider.width()));
      }
      else
      {
        return QRect();
      }

      if(sc == QStyle::SC_ScrollBarSlider)
        return slider;

      if(sc == QStyle::SC_ScrollBarSubPage)
        return ret.adjusted(0, 0, slider.left() - ret.right(), 0);
      if(sc == QStyle::SC_ScrollBarAddPage)
        return ret.adjusted(slider.right() - ret.left(), 0, 0, 0);
    }
    else
    {
      if(sc == QStyle::SC_ScrollBarSubLine)
        return ret.adjusted(0, 0, 0, -ret.height() + Constants::ScrollButtonDim);
      if(sc == QStyle::SC_ScrollBarAddLine)
        return ret.adjusted(0, ret.height() - Constants::ScrollButtonDim, 0, 0);

      const int buttonAdjust = Constants::ScrollButtonDim + Constants::ScrollBarMargin;
      ret.adjust(0, buttonAdjust, 0, -buttonAdjust);

      if(sc == QStyle::SC_ScrollBarGroove)
        return ret;

      QRect slider = ret;

      if(scroll->maximum > scroll->minimum)
      {
        int sliderSize = qMax(Constants::ScrollBarMin,
                              (scroll->pageStep * ret.height()) / (range + scroll->pageStep));

        slider.setHeight(qMin(slider.height(), sliderSize));
        slider.moveTop(ret.top() + (qreal(scroll->sliderPosition) / qreal(range)) *
                                       (ret.height() - slider.height()));
      }
      else
      {
        return QRect();
      }

      if(sc == QStyle::SC_ScrollBarSlider)
        return slider;

      if(sc == QStyle::SC_ScrollBarSubPage)
        return ret.adjusted(0, 0, 0, slider.top() - ret.bottom());
      if(sc == QStyle::SC_ScrollBarAddPage)
        return ret.adjusted(0, slider.bottom() - ret.top(), 0, 0);
    }

    return opt->rect;
  }
  else if(cc == QStyle::CC_Slider)
  {
    QRect ret = opt->rect;
    ret.adjust(1, 1, -1, -1);
    if(sc == QStyle::SC_SliderGroove)
    {
      int toGrooveHeightHalfReduction = (ret.height() - Constants::SliderGrooveHeight) / 2;
      // QSlider code handles only moving the handle up to where it abutts the groove in either
      // direction, reducing it here breaks that calculation. We'll do it visually when actually
      // painting
      ret.adjust(0, toGrooveHeightHalfReduction, 0, -toGrooveHeightHalfReduction);
    }
    else if(sc == QStyle::SC_SliderHandle)
    {
      const QAbstractSlider *slider = qobject_cast<const QAbstractSlider *>(widget);
      int sliderMin = slider->minimum();
      int sliderMax = slider->maximum();
      int sliderPos = slider->sliderPosition();
      qreal posUNorm = (qreal)(sliderPos - sliderMin) / (qreal)(sliderMax - sliderMin);
      int grooveLeft = ret.left() + Constants::SliderHandleHalfWidth;
      int grooveRight = ret.right() - Constants::SliderHandleHalfWidth;
      int grooveWidth = grooveRight - grooveLeft;
      int sliderX = (int)(posUNorm * (qreal)grooveWidth) + grooveLeft;
      // since sizes are inclusive, x-w to x+w results in 2w+1 width (w pixels either side + the x
      // pixel itself), so we make this favour the right side so it's w-1 pixels to the left, the x
      // pixel and then w pixels to the right, for a total of 2w
      ret.setLeft(sliderX - Constants::SliderHandleHalfWidth + 1);
      ret.setRight(sliderX + Constants::SliderHandleHalfWidth);
    }
    return ret;
  }
  else if(cc == QStyle::CC_ComboBox)
  {
    QRect rect = opt->rect;

    if(sc == QStyle::SC_ComboBoxFrame || sc == QStyle::SC_ComboBoxListBoxPopup)
      return rect;

    const QStyleOptionComboBox *combo = qstyleoption_cast<const QStyleOptionComboBox *>(opt);

    if(combo->subControls & QStyle::SC_ComboBoxFrame)
    {
      rect.adjust(Constants::ComboMargin, Constants::ComboMargin, -Constants::ComboMargin,
                  -Constants::ComboMargin);
    }

    if(sc == QStyle::SC_ComboBoxEditField)
      return rect.adjusted(0, 0, -Constants::ComboArrowDim, 0);

    if(sc == QStyle::SC_ComboBoxArrow)
      return rect.adjusted(rect.width() - Constants::ComboArrowDim, 0, 0, 0);
  }
  else if(cc == QStyle::CC_SpinBox)
  {
    QRect rect = opt->rect;

    if(sc == QStyle::SC_SpinBoxFrame)
      return rect;

    rect.adjust(Constants::ButtonBorder, Constants::ButtonBorder, -Constants::ButtonBorder,
                -Constants::ButtonBorder);

    if(sc == QStyle::SC_SpinBoxEditField)
      return rect.adjusted(Constants::SpinMargin, Constants::SpinMargin,
                           -Constants::SpinButtonDim - Constants::SpinMargin, -Constants::SpinMargin);

    rect.adjust(rect.width() - Constants::SpinButtonDim, 0, 0, 0);

    int buttonHeight = rect.height() / 2;

    if(sc == QStyle::SC_SpinBoxUp)
      return rect.adjusted(0, 0, 0, -(rect.height() - buttonHeight));

    if(sc == QStyle::SC_SpinBoxDown)
      return rect.adjusted(0, rect.height() - buttonHeight, 0, 0);

    return opt->rect;
  }

  return RDTweakedNativeStyle::subControlRect(cc, opt, sc, widget);
}

QRect RDStyle::subElementRect(SubElement element, const QStyleOption *opt, const QWidget *widget) const
{
  if(element == QStyle::SE_PushButtonContents || element == QStyle::SE_PushButtonFocusRect)
  {
    const int border = Constants::ButtonBorder;
    return opt->rect.adjusted(border, border, -2 * border, -2 * border);
  }
  else if(element == QStyle::SE_RadioButtonFocusRect || element == QStyle::SE_CheckBoxFocusRect)
  {
    return opt->rect;
  }
  else if(element == QStyle::SE_RadioButtonIndicator || element == QStyle::SE_CheckBoxIndicator ||
          element == QStyle::SE_ItemViewItemCheckIndicator)
  {
    QRect ret = opt->rect;

    if(element == QStyle::SE_ItemViewItemCheckIndicator)
      ret.setLeft(ret.left() + 4);
    ret.setWidth(Constants::CheckWidth);

    int extra = ret.height() - Constants::CheckHeight;

    ret.setTop(ret.top() + extra / 2);
    ret.setHeight(Constants::CheckHeight);

    return ret;
  }
  else if(element == QStyle::SE_RadioButtonContents || element == QStyle::SE_CheckBoxContents)
  {
    QRect ret = opt->rect;

    ret.setLeft(ret.left() + Constants::CheckWidth + Constants::CheckMargin);

    return ret;
  }
  else if(element == QStyle::SE_TabWidgetTabPane || element == QStyle::SE_TabWidgetTabContents ||
          element == QStyle::SE_TabWidgetTabBar)
  {
    const QStyleOptionTabWidgetFrame *tabwidget =
        qstyleoption_cast<const QStyleOptionTabWidgetFrame *>(opt);

    QRect rect = tabwidget->rect;

    QRect barRect = rect;
    barRect.setSize(tabwidget->tabBarSize);

    barRect.setWidth(qMin(barRect.width(), tabwidget->rect.width() -
                                               tabwidget->leftCornerWidgetSize.width() -
                                               tabwidget->rightCornerWidgetSize.width()));

    if(element == QStyle::SE_TabWidgetTabBar)
      return barRect;

    rect.setTop(rect.top() + barRect.height());

    if(element == QStyle::SE_TabWidgetTabPane)
      return rect;

    const int border = Constants::TabWidgetBorder;
    rect.adjust(border, 0, -border, -border);

    return rect;
  }
  else if(element == QStyle::SE_TabBarTabLeftButton || element == QStyle::SE_TabBarTabRightButton)
  {
    const QStyleOptionTab *tab = qstyleoption_cast<const QStyleOptionTab *>(opt);

    QRect ret = tab->rect;

    if(element == SE_TabBarTabLeftButton)
    {
      ret.setSize(tab->leftButtonSize);
      ret.moveLeft(Constants::TabMargin);
    }
    else if(element == SE_TabBarTabRightButton)
    {
      ret.setSize(tab->rightButtonSize);
      ret.moveRight(tab->rect.right() - Constants::TabMargin);
    }

    // centre it vertically

    ret.moveTop((tab->rect.height() - ret.height()) / 2);

    return ret;
  }
  else if(element == QStyle::SE_HeaderLabel)
  {
    return opt->rect;
  }

  return RDTweakedNativeStyle::subElementRect(element, opt, widget);
}

QSize RDStyle::sizeFromContents(ContentsType type, const QStyleOption *opt, const QSize &size,
                                const QWidget *widget) const
{
  if(type == CT_PushButton || type == CT_ToolButton)
  {
    const QStyleOptionButton *button = qstyleoption_cast<const QStyleOptionButton *>(opt);
    const QStyleOptionToolButton *toolbutton = qstyleoption_cast<const QStyleOptionToolButton *>(opt);

    QSize ret = size;

    // only for pushbuttons with text, ensure a minimum size
    if(type == CT_PushButton && button && !button->text.isEmpty())
    {
      ret.setWidth(qMax(50, ret.width()));
      ret.setHeight(qMax(15, ret.height()));
    }
    else if(type == CT_ToolButton && toolbutton)
    {
      ret = adjustToolButtonSize(toolbutton, size, widget);
    }

    // Unity's controls are deliberately compact, while the RenderDoc themes retain their
    // existing roomier metrics.
    if(m_Scheme == Ultra)
    {
      ret.setHeight(qMax(type == CT_ToolButton ? 20 : 18, ret.height() + 2));
      ret.setWidth(ret.width() + 6);
    }
    else
    {
      ret.setHeight(ret.height() + Constants::ButtonMargin + Constants::ButtonBorder * 2);
      ret.setWidth(ret.width() + Constants::ButtonMargin + Constants::ButtonBorder * 2);
    }

    return ret;
  }
  else if(type == CT_TabBarTab)
  {
    // have a maximum size for tabs
    QSize ret = size.boundedTo(QSize(Constants::TabMaxWidth, INT_MAX))
                    .expandedTo(QSize(Constants::TabMinWidth, 0)) +
                QSize(Constants::TabMargin * 2, 0);
    if(m_Scheme == Ultra)
      ret.setHeight(qMax(20, ret.height()));
    return ret;
  }
  else if(type == CT_CheckBox || type == CT_RadioButton)
  {
    const QStyleOptionButton *button = qstyleoption_cast<const QStyleOptionButton *>(opt);

    QSize ret = size;

    // set minimum height for check/radio
    ret.setHeight(qMax(ret.height(), Constants::CheckHeight) + Constants::HighlightBorder);

    // add width for the check/radio and a gap before the text/icon
    ret.setWidth(Constants::CheckWidth + Constants::CheckMargin + ret.width());

    return ret;
  }
  else if(type == CT_LineEdit)
  {
    QSize ret = size;

    const QStyleOptionFrame *frame = qstyleoption_cast<const QStyleOptionFrame *>(opt);

    if(frame && frame->lineWidth > 0)
    {
      ret.setWidth(Constants::ButtonBorder * 2 + ret.width());
      ret.setHeight(Constants::ButtonBorder * 2 + ret.height());
    }

    if(m_Scheme == Ultra)
      ret.setHeight(qMax(18, ret.height()));
    return ret;
  }
  else if(type == CT_GroupBox || type == CT_ScrollBar || type == CT_ProgressBar || type == CT_Splitter)
  {
    return size;
  }
  else if(type == CT_ComboBox)
  {
    QSize ret = size;

    // make room for both the down arrow button and a potential scrollbar
    ret.setWidth(Constants::ButtonBorder * 2 + Constants::ComboMargin * 2 +
                 Constants::ComboArrowDim + Constants::ScrollButtonDim + ret.width());
    ret.setHeight(Constants::ButtonBorder * 2 + Constants::ComboMargin * 2 + ret.height());

    if(m_Scheme == Ultra)
      ret.setHeight(qMax(18, ret.height()));

    return ret;
  }
  else if(type == CT_SpinBox)
  {
    QSize ret = size;

    const int margin = Constants::SpinMargin + Constants::ButtonBorder;
    ret.setWidth(margin * 2 + Constants::SpinButtonDim + ret.width());
    ret.setHeight(margin * 2 + ret.height());

    if(m_Scheme == Ultra)
      ret.setHeight(qMax(18, ret.height()));

    return ret;
  }
  else if(type == CT_MenuItem)
  {
    QSize ret = size;

    ret.setWidth(ret.width() + 2 * Constants::MenuBarMargin);
    ret.setHeight(ret.height() + (m_Scheme == Ultra ? 2 : Constants::MenuBarMargin));
    if(m_Scheme == Ultra)
      ret.setHeight(qMax(20, ret.height()));

    const QStyleOptionMenuItem *menuitem = qstyleoption_cast<const QStyleOptionMenuItem *>(opt);

    // add more room for items with shortcuts
    if(menuitem->text.contains(QLatin1Char('\t')))
      ret.setWidth(ret.width() + 4 * Constants::MenuBarMargin);

    // add room for an icon
    if(menuitem->maxIconWidth || menuitem->menuHasCheckableItems)
      ret.setWidth(ret.width() + Constants::MenuBarMargin +
                   std::max(menuitem->maxIconWidth, Constants::MenuBarIconSize));

    if(menuitem->menuItemType == QStyleOptionMenuItem::SubMenu)
      ret.setWidth(ret.width() + Constants::MenuSubmenuWidth);

    ret = ret.expandedTo(QSize(Constants::MenuBarMinimumWidth, 0));

    return ret;
  }
  else if(type == CT_MenuBarItem)
  {
    const QStyleOptionMenuItem *menuitem = qstyleoption_cast<const QStyleOptionMenuItem *>(opt);
    int iconSize = pixelMetric(QStyle::PM_SmallIconSize, opt, widget);
    QSize sz = menuitem->fontMetrics.size(Qt::TextShowMnemonic, menuitem->text);

    if(!menuitem->icon.isNull() || menuitem->checkType != QStyleOptionMenuItem::NotCheckable)
    {
      sz.setWidth(sz.width() + Constants::MenuBarMargin + iconSize);
      sz = sz.expandedTo(QSize(1, iconSize));
    }

    sz += QSize(Constants::MenuBarMargin * 2,
                m_Scheme == Ultra ? 2 : Constants::MenuBarMargin);

    if(m_Scheme == Ultra)
      sz.setHeight(qMax(20, sz.height()));

    return sz;
  }
  else if(type == CT_MenuBar || type == CT_Menu)
  {
    return size;
  }
  else if(type == CT_HeaderSection)
  {
    const QStyleOptionHeader *header = qstyleoption_cast<const QStyleOptionHeader *>(opt);
    int iconSize = pixelMetric(QStyle::PM_SmallIconSize, opt, widget);
    QSize sz = header->fontMetrics.size(Qt::TextShowMnemonic, header->text);

    if(!header->icon.isNull())
    {
      sz.setWidth(sz.width() + Constants::ItemHeaderMargin + iconSize);
      sz = sz.expandedTo(QSize(1, iconSize));
    }

    if(header->sortIndicator != QStyleOptionHeader::None)
    {
      sz += QSize(Constants::ItemHeaderMargin + Constants::SpinButtonDim, 0);
    }

    sz += QSize(Constants::ItemHeaderMargin * 2,
                m_Scheme == Ultra ? 2 : Constants::ItemHeaderMargin);

    if(m_Scheme == Ultra)
      sz.setHeight(qMax(20, sz.height()));

    return sz;
  }

  return RDTweakedNativeStyle::sizeFromContents(type, opt, size, widget);
}

int RDStyle::pixelMetric(PixelMetric metric, const QStyleOption *opt, const QWidget *widget) const
{
  if(metric == QStyle::PM_ButtonShiftHorizontal || metric == QStyle::PM_ButtonShiftVertical)
  {
    if(m_Scheme == Ultra)
      return 0;

    if(opt && (opt->state & State_AutoRaise) == 0)
      return 1;
  }

  if(metric == PM_ScrollBarExtent)
    return Constants::ScrollButtonDim + 2;
  // not used for rendering but just as an estimate of how small a progress bar can get
  if(metric == PM_ProgressBarChunkWidth)
    return 10;

  if(metric == PM_SplitterWidth)
    return 5;

  if(m_Scheme == Ultra && metric == PM_TreeViewIndentation)
    return 15;

  if(m_Scheme == Ultra && (metric == PM_SmallIconSize || metric == PM_ToolBarIconSize ||
                            metric == PM_ButtonIconSize))
    return 16;

  if(m_Scheme == Ultra && metric == PM_ToolTipLabelFrameWidth)
    return 1;

  if(metric == PM_MenuBarHMargin || metric == PM_MenuBarVMargin)
    return 1;

  if(metric == PM_MenuBarPanelWidth || metric == PM_MenuPanelWidth)
    return 1;

  if(metric == PM_MenuHMargin || metric == PM_MenuVMargin)
    return 0;

  if(metric == PM_MenuBarItemSpacing)
    return 0;

  if(metric == PM_MenuDesktopFrameWidth)
    return 0;

  if(metric == PM_SubMenuOverlap)
    return 0;

  if(metric == PM_MenuButtonIndicator)
    return Constants::ComboArrowDim;

  if(metric == PM_TabBarTabOverlap)
    return 0;

  if(metric == PM_TabBarTabHSpace)
    return Constants::TabMargin;

  if(metric == PM_IndicatorWidth)
    return Constants::CheckWidth + Constants::CheckMargin;

  if(metric == PM_IndicatorHeight)
    return Constants::CheckHeight + Constants::CheckMargin;

  return RDTweakedNativeStyle::pixelMetric(metric, opt, widget);
}

int RDStyle::styleHint(StyleHint stylehint, const QStyleOption *opt, const QWidget *widget,
                       QStyleHintReturn *returnData) const
{
  if(stylehint == QStyle::SH_EtchDisabledText || stylehint == QStyle::SH_DitherDisabledText)
    return 0;

  if(stylehint == QStyle::SH_ComboBox_PopupFrameStyle)
    return QFrame::StyledPanel | QFrame::Plain;

  if(stylehint == QStyle::SH_ComboBox_Popup)
    return 0;

  if(stylehint == SH_ToolTipLabel_Opacity)
    return 255;

  if(m_Scheme == Ultra && stylehint == SH_ToolTip_WakeUpDelay)
    return 400;

  if(stylehint == SH_UnderlineShortcut)
    return 0;

  if(stylehint == SH_MessageBox_CenterButtons)
    return 0;
  if(stylehint == SH_ProgressDialog_CenterCancelButton)
    return 1;

  if(stylehint == SH_ProgressDialog_TextLabelAlignment)
    return Qt::AlignCenter;

  if(stylehint == SH_Splitter_OpaqueResize)
    return 1;

  if(stylehint == SH_MenuBar_MouseTracking || stylehint == SH_Menu_MouseTracking ||
     stylehint == SH_MenuBar_AltKeyNavigation || stylehint == SH_MainWindow_SpaceBelowMenuBar)
    return 1;

  if(stylehint == SH_Menu_FlashTriggeredItem || stylehint == SH_Menu_KeyboardSearch ||
     stylehint == SH_Menu_FadeOutOnHide || stylehint == SH_Menu_AllowActiveAndDisabled)
    return 0;

  if(stylehint == SH_Menu_SubMenuPopupDelay || stylehint == SH_Menu_SubMenuSloppyCloseTimeout)
    return 500;

  if(stylehint == SH_Menu_SubMenuResetWhenReenteringParent ||
     stylehint == SH_Menu_SubMenuDontStartSloppyOnLeave)
    return 0;

  if(stylehint == SH_Menu_SubMenuUniDirection || stylehint == SH_Menu_SubMenuUniDirectionFailCount)
    return 0;

  if(stylehint == SH_Menu_SubMenuSloppySelectOtherActions)
    return 1;

  if(stylehint == QStyle::SH_ItemView_ArrowKeysNavigateIntoChildren)
    return 1;

  if(stylehint == QStyle::SH_TabBar_ElideMode)
    return Qt::ElideRight;

  return RDTweakedNativeStyle::styleHint(stylehint, opt, widget, returnData);
}

QIcon RDStyle::standardIcon(StandardPixmap standardIcon, const QStyleOption *opt,
                            const QWidget *widget) const
{
  return RDTweakedNativeStyle::standardIcon(standardIcon, opt, widget);
}

void RDStyle::drawComplexControl(ComplexControl control, const QStyleOptionComplex *opt,
                                 QPainter *p, const QWidget *widget) const
{
  // Ultra also renders auto-raise tool buttons so toolbar hover/pressed states use the same
  // palette as the rest of the theme.
  if(control == QStyle::CC_ToolButton &&
     ((opt->state & State_AutoRaise) == 0 || m_Scheme == Ultra))
  {
    drawRoundedRectBorder(opt, p, widget, QPalette::Button, true);

    const QStyleOptionToolButton *toolbutton = qstyleoption_cast<const QStyleOptionToolButton *>(opt);

    QStyleOptionToolButton labelTextIcon = *toolbutton;
    labelTextIcon.rect = subControlRect(control, opt, SC_ToolButton, widget);

    // draw the label text/icon
    proxy()->drawControl(CE_ToolButtonLabel, &labelTextIcon, p, widget);

    // draw the menu arrow, if there is one
    if(shouldDrawToolButtonMenuArrow(toolbutton))
    {
      QStyleOptionToolButton menu = *toolbutton;
      menu.rect = subControlRect(control, opt, SC_ToolButtonMenu, widget);

      if(m_Scheme == Ultra &&
         (toolbutton->features & QStyleOptionToolButton::MenuButtonPopup) &&
         (opt->state & (State_MouseOver | State_Sunken | State_On)))
      {
        p->save();
        p->setPen(QPen(UltraColors::color(UltraColors::DefaultBorder), 1.0));
        p->drawLine(menu.rect.topLeft(), menu.rect.bottomLeft());
        p->restore();
      }

      if(m_Scheme == Ultra)
        menu.rect.adjust(2, 0, 0, 0);
      proxy()->drawPrimitive(PE_IndicatorArrowDown, &menu, p, widget);
    }

    return;
  }
  else if(control == CC_GroupBox)
  {
    // when drawing the border don't apply any states intended for the checkbox
    QStyleOptionComplex frame = *opt;
    frame.state &=
        ~(QStyle::State_Sunken | QStyle::State_MouseOver | QStyle::State_On | QStyle::State_Off);
    drawRoundedRectBorder(&frame, p, widget, QPalette::Window, false);

    const QStyleOptionGroupBox *group = qstyleoption_cast<const QStyleOptionGroupBox *>(opt);

    QRect labelRect = proxy()->subControlRect(CC_GroupBox, opt, QStyle::SC_GroupBoxLabel, widget);

    labelRect.adjust(Constants::GroupHMargin, Constants::GroupVMargin, Constants::GroupHMargin,
                     Constants::GroupVMargin);

    QColor textColor = group->textColor;
    QPalette::ColorRole penRole = QPalette::WindowText;

    if(textColor.isValid())
    {
      p->setPen(textColor);
      penRole = QPalette::NoRole;
    }

    proxy()->drawItemText(p, labelRect, Qt::AlignLeft | Qt::AlignTop | Qt::TextHideMnemonic,
                          group->palette, group->state & State_Enabled, group->text, penRole);

    labelRect.setRight(subControlRect(CC_GroupBox, opt, QStyle::SC_GroupBoxFrame, widget).right());
    labelRect.adjust(-Constants::GroupHMargin / 2, 0, -Constants::GroupHMargin, 0);

    p->setPen(QPen(opt->palette.brush(m_Scheme == Light ? QPalette::Mid : QPalette::Midlight), 1.0));
    p->drawLine(labelRect.bottomLeft(), labelRect.bottomRight());

    if(opt->subControls & QStyle::SC_GroupBoxCheckBox)
    {
      QRect checkBoxRect = proxy()->subControlRect(CC_GroupBox, opt, SC_GroupBoxCheckBox, widget);

      QStyleOptionButton box;
      (QStyleOption &)box = *(QStyleOption *)opt;
      box.rect = checkBoxRect;
      proxy()->drawPrimitive(PE_IndicatorCheckBox, &box, p, widget);
    }

    return;
  }
  else if(control == QStyle::CC_ScrollBar)
  {
    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    p->fillRect(opt->rect, opt->palette.brush(QPalette::Window));
    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, QColor(0, 0, 0, 13));

    QBrush hoverBrush;
    QBrush sliderBrush;

    if(m_Scheme == Ultra)
    {
      const bool sliderHovered = (opt->state & State_MouseOver) &&
                                 (opt->activeSubControls & QStyle::SC_ScrollBarSlider);
      sliderBrush = UltraColors::color(sliderHovered
                                           ? UltraColors::ScrollbarThumbHover
                                           : UltraColors::ScrollbarThumb);
      hoverBrush = UltraColors::color(UltraColors::AppToolbarBackgroundHover);
    }
    else if(m_Scheme == Light)
    {
      sliderBrush = opt->palette.brush(QPalette::Dark);
      hoverBrush = opt->palette.brush(QPalette::Midlight);
    }
    else
    {
      sliderBrush = opt->palette.brush(QPalette::Text);
      hoverBrush = opt->palette.brush(QPalette::Light);
    }

    const QStyleOptionSlider *scroll = qstyleoption_cast<const QStyleOptionSlider *>(opt);

    if(scroll)
    {
      const int margin = Constants::ScrollBarMargin;

      {
        p->setPen(QPen(sliderBrush, 2.5));

        QRectF rect = proxy()->subControlRect(CC_ScrollBar, opt, QStyle::SC_ScrollBarSubLine, widget);

        rect = rect.adjusted(margin, margin, -margin, -margin);

        if(scroll->orientation == Qt::Vertical)
          rect.moveTop(rect.top() + rect.height() / 4);
        else
          rect.moveLeft(rect.left() + rect.width() / 4);

        QPainterPath path;
        QPolygonF poly;

        if(scroll->orientation == Qt::Vertical)
        {
          QPointF pt = rect.center();
          pt.setX(rect.left());
          poly << pt;

          pt = rect.center();
          pt.setY(rect.top());
          poly << pt;

          pt = rect.center();
          pt.setX(rect.right());
          poly << pt;
        }
        else
        {
          QPointF pt = rect.center();
          pt.setY(rect.top());
          poly << pt;

          pt = rect.center();
          pt.setX(rect.left());
          poly << pt;

          pt = rect.center();
          pt.setY(rect.bottom());
          poly << pt;
        }

        path.addPolygon(poly);

        p->drawPath(path);
      }

      {
        p->setPen(QPen(sliderBrush, 2.5));

        QRectF rect = proxy()->subControlRect(CC_ScrollBar, opt, QStyle::SC_ScrollBarAddLine, widget);

        rect = rect.adjusted(margin, margin, -margin, -margin);

        if(scroll->orientation == Qt::Vertical)
          rect.moveBottom(rect.bottom() - rect.height() / 4);
        else
          rect.moveRight(rect.right() - rect.width() / 4);

        QPainterPath path;
        QPolygonF poly;

        if(scroll->orientation == Qt::Vertical)
        {
          QPointF pt = rect.center();
          pt.setX(rect.left());
          poly << pt;

          pt = rect.center();
          pt.setY(rect.bottom());
          poly << pt;

          pt = rect.center();
          pt.setX(rect.right());
          poly << pt;
        }
        else
        {
          QPointF pt = rect.center();
          pt.setY(rect.top());
          poly << pt;

          pt = rect.center();
          pt.setX(rect.right());
          poly << pt;

          pt = rect.center();
          pt.setY(rect.bottom());
          poly << pt;
        }

        path.addPolygon(poly);

        p->drawPath(path);
      }
    }

    QStyle::State activeHover = State_MouseOver | State_Active | State_Enabled;
    if(m_Scheme != Ultra && (opt->state & activeHover) == activeHover)
    {
      QRect hoverRect =
          proxy()
              ->subControlRect(CC_ScrollBar, opt, QStyle::SC_ScrollBarAddPage, widget)
              .united(subControlRect(CC_ScrollBar, opt, QStyle::SC_ScrollBarSubPage, widget));

      QPainterPath path;
      path.addRoundedRect(hoverRect, Constants::ScrollBarRadius, Constants::ScrollBarRadius);

      p->fillPath(path, hoverBrush);
    }

    QRect slider = proxy()->subControlRect(CC_ScrollBar, opt, QStyle::SC_ScrollBarSlider, widget);

    if(slider.isValid() && (opt->state & State_Enabled))
    {
      QPainterPath path;
      const qreal radius = m_Scheme == Ultra ? 3.0 : Constants::ScrollBarRadius;
      path.addRoundedRect(slider, radius, radius);

      if((opt->state & State_Sunken) && m_Scheme != Ultra)
        p->fillPath(path, opt->palette.brush(QPalette::Highlight));
      else
        p->fillPath(path, sliderBrush);

      if(m_Scheme == Ultra)
      {
        p->setPen(QPen(UltraColors::color(UltraColors::ScrollbarThumbBorder), 1.0));
        p->drawPath(path.translated(QPointF(0.5, 0.5)));
      }
    }

    p->restore();

    return;
  }
  else if(control == QStyle::CC_Slider)
  {
    QRect grooveRect = subControlRect(control, opt, QStyle::SC_SliderGroove, widget);

    if(m_Scheme == Ultra)
    {
      p->save();
      p->setPen(QPen(UltraColors::color(UltraColors::SliderGroove), 1.0));
    }

    p->drawLine(QLine(grooveRect.x() + Constants::SliderHandleHalfWidth + 1,
                      grooveRect.y() + grooveRect.height() / 2,
                      grooveRect.right() - Constants::SliderHandleHalfWidth,
                      grooveRect.y() + grooveRect.height() / 2));

    if(m_Scheme == Ultra)
      p->restore();

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    QRect handleRect = subControlRect(control, opt, QStyle::SC_SliderHandle, widget);
    QBrush handleBrush;
    if(m_Scheme == Ultra)
    {
      QRgb handleColor = UltraColors::SliderThumb;
      if(!(opt->state & State_Enabled))
        handleColor = UltraColors::SliderThumbDisabled;
      else if((opt->state & State_MouseOver) &&
              (opt->activeSubControls & QStyle::SC_SliderHandle))
        handleColor = UltraColors::SliderThumbHover;
      handleBrush = UltraColors::color(handleColor);

      if(opt->state & State_HasFocus)
      {
        QPainterPath halo;
        halo.addRoundedRect(handleRect.adjusted(-2, -2, 2, 2), 5.0, 5.0);
        p->fillPath(halo, QColor(16, 111, 205, 128));
      }
    }
    else
    {
      handleBrush = (m_Scheme == Light) ? opt->palette.brush(QPalette::Dark)
                                        : opt->palette.brush(QPalette::Text);
    }
    QPainterPath path;
    const qreal radius = m_Scheme == Ultra ? 3.0 : Constants::SliderHandleCornerRadius;
    path.addRoundedRect(handleRect, radius, radius);
    p->fillPath(path, handleBrush);

    p->restore();

    return;
  }
  else if(control == QStyle::CC_ComboBox)
  {
    const QStyleOptionComboBox *combo = qstyleoption_cast<const QStyleOptionComboBox *>(opt);

    if(combo->subControls & QStyle::SC_ComboBoxFrame)
    {
      drawRoundedRectBorder(opt, p, widget, QPalette::Base, false);
    }

    QRectF rect = proxy()->subControlRect(control, opt, QStyle::SC_ComboBoxArrow, widget);

    if(!(combo->subControls & QStyle::SC_ComboBoxFrame))
      p->fillRect(rect, opt->palette.brush(QPalette::Base));

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    rect.setTop(rect.top() + rect.height() / 2.0 - rect.width() / 2.0);
    rect.setHeight(rect.width());

    {
      qreal penWidth = 1.5;
      p->setPen(QPen(m_Scheme == Ultra
                         ? opt->palette.brush(QPalette::WindowText)
                         : outlineBrush(opt->palette),
                     penWidth));

      QPainterPath path;
      QPolygonF poly;

      QPointF pt = rect.center();
      pt.setX(rect.left() + penWidth);
      poly << pt;

      pt = rect.center();
      pt.setY(rect.bottom() - penWidth);
      poly << pt;

      pt = rect.center();
      pt.setX(rect.right() - penWidth);
      poly << pt;

      path.addPolygon(poly);

      p->drawPath(path);
    }

    p->restore();

    return;
  }
  else if(control == QStyle::CC_SpinBox)
  {
    {
      QStyleOption o = *opt;
      o.state &= ~State_Sunken;
      drawRoundedRectBorder(&o, p, widget, QPalette::Base, false);
    }

    QRect rect = opt->rect;
    rect.adjust(Constants::ButtonBorder, Constants::ButtonBorder, -Constants::ButtonBorder,
                -Constants::ButtonBorder);

    rect.adjust(0, 0, -Constants::SpinButtonDim, 0);

    p->save();

    p->setPen(QPen(outlineBrush(opt->palette), 1.0));

    p->drawLine(rect.topRight(), rect.bottomRight());

    rect = proxy()->subControlRect(control, opt, QStyle::SC_SpinBoxUp, widget);

    p->setClipRect(rect);

    const QStyleOptionSpinBox *spinbox = qstyleoption_cast<const QStyleOptionSpinBox *>(opt);

    {
      QPainterPath path;
      path.addRoundedRect(rect, 1.0, 1.0);

      if((opt->state & State_Sunken) && (spinbox->activeSubControls & QStyle::SC_SpinBoxUp))
        p->fillPath(path, opt->palette.brush(QPalette::Midlight));
      else
        p->fillPath(path, opt->palette.brush(QPalette::Button));
    }

    p->drawLine(rect.bottomLeft(), rect.bottomRight());

    p->setRenderHint(QPainter::Antialiasing);

    QPalette::ColorGroup group = QPalette::Disabled;
    if(spinbox->stepEnabled & QAbstractSpinBox::StepUpEnabled)
      group = QPalette::Normal;

    qreal penWidth = 1.5;
    p->setPen(QPen(opt->palette.brush(group, QPalette::WindowText), penWidth));

    {
      QRectF arrowRect = QRectF(rect);
      arrowRect.adjust(0.5, 0.5, -0.5, 0.5);

      QPainterPath path;
      QPolygonF poly;

      QPointF pt = arrowRect.center();
      pt.setX(arrowRect.left() + penWidth);
      poly << pt;

      pt = arrowRect.center();
      pt.setY(arrowRect.top() + penWidth);
      poly << pt;

      pt = arrowRect.center();
      pt.setX(arrowRect.right() - penWidth);
      poly << pt;

      path.addPolygon(poly);

      p->drawPath(path);
    }

    rect = proxy()->subControlRect(control, opt, QStyle::SC_SpinBoxDown, widget);

    p->setClipRect(rect);

    {
      QPainterPath path;
      path.addRoundedRect(rect, 1.0, 1.0);

      if((opt->state & State_Sunken) && (spinbox->activeSubControls & QStyle::SC_SpinBoxDown))
        p->fillPath(path, opt->palette.brush(QPalette::Midlight));
      else
        p->fillPath(path, opt->palette.brush(QPalette::Button));
    }

    group = QPalette::Disabled;
    if(spinbox->stepEnabled & QAbstractSpinBox::StepDownEnabled)
      group = QPalette::Normal;

    p->setPen(QPen(opt->palette.brush(group, QPalette::WindowText), penWidth));

    {
      QRectF arrowRect = QRectF(rect);
      arrowRect.adjust(0.5, -0.5, -0.5, -0.5);

      QPainterPath path;
      QPolygonF poly;

      QPointF pt = arrowRect.center();
      pt.setX(arrowRect.left() + penWidth);
      poly << pt;

      pt = arrowRect.center();
      pt.setY(arrowRect.bottom() - penWidth);
      poly << pt;

      pt = arrowRect.center();
      pt.setX(arrowRect.right() - penWidth);
      poly << pt;

      path.addPolygon(poly);

      p->drawPath(path);
    }

    p->restore();

    return;
  }

  return RDTweakedNativeStyle::drawComplexControl(control, opt, p, widget);
}

static void drawX(const QStyleOption *opt, QPainter *p, const QRectF &rect)
{
  QPainterPath checkpath;
  QPolygonF poly;

  // Left side:
  //
  //             X
  //             | CheckCornerSize
  //             X
  //              \\   innerSizeX, innerSizeY
  //               \\  (width and height)
  // CheckHeight    X
  //               /
  //              /
  //             X
  //             | CheckCornerSize
  //             X

  // Top side:
  //
  // X---X      X----X
  //      \\   /
  //       \\ /
  //         X

  Q_ASSERT(rect.height() == rect.width());

  const float innerSize = float(rect.height() - Constants::CheckCornerSize * 2) / 2.0f;

  const float totalSize = innerSize * 2 + Constants::CheckCornerSize * 2;
  Q_ASSERT(totalSize == rect.height());

  // left edge
  QPointF pt = rect.topLeft();
  poly << pt;
  pt.setY(pt.y() + Constants::CheckCornerSize);
  poly << pt;
  pt.setY(pt.y() + innerSize);
  pt.setX(pt.x() + innerSize);
  poly << pt;
  pt.setY(pt.y() + innerSize);
  pt.setX(pt.x() - innerSize);
  poly << pt;
  pt.setY(pt.y() + Constants::CheckCornerSize);
  poly << pt;

  // bottom edge
  pt.setX(pt.x() + Constants::CheckCornerSize);
  poly << pt;
  pt.setX(pt.x() + innerSize);
  pt.setY(pt.y() - innerSize);
  poly << pt;
  pt.setX(pt.x() + innerSize);
  pt.setY(pt.y() + innerSize);
  poly << pt;
  pt.setX(pt.x() + Constants::CheckCornerSize);
  poly << pt;

  // right edge
  pt.setY(pt.y() - Constants::CheckCornerSize);
  poly << pt;
  pt.setX(pt.x() - innerSize);
  pt.setY(pt.y() - innerSize);
  poly << pt;
  pt.setX(pt.x() + innerSize);
  pt.setY(pt.y() - innerSize);
  poly << pt;
  pt.setY(pt.y() - Constants::CheckCornerSize);
  poly << pt;

  // top edge
  pt.setX(pt.x() - Constants::CheckCornerSize);
  poly << pt;
  pt.setX(pt.x() - innerSize);
  pt.setY(pt.y() + innerSize);
  poly << pt;
  pt.setX(pt.x() - innerSize);
  pt.setY(pt.y() - innerSize);
  poly << pt;
  pt.setX(pt.x() - Constants::CheckCornerSize);
  poly << pt;

  checkpath.addPolygon(poly);
  p->fillPath(checkpath, opt->palette.brush(QPalette::ButtonText));
}

static void drawCheckmark(const QStyleOption *opt, QPainter *p, const QRectF &rect)
{
  QPainterPath path;
  path.moveTo(rect.left() + rect.width() * 0.12, rect.top() + rect.height() * 0.52);
  path.lineTo(rect.left() + rect.width() * 0.40, rect.top() + rect.height() * 0.78);
  path.lineTo(rect.left() + rect.width() * 0.90, rect.top() + rect.height() * 0.20);

  QPen pen(opt->palette.brush(QPalette::HighlightedText), 1.8, Qt::SolidLine, Qt::RoundCap,
           Qt::RoundJoin);
  p->strokePath(path, pen);
}

static void drawMixedMark(const QStyleOption *opt, QPainter *p, const QRectF &rect)
{
  QPen pen(opt->palette.brush(QPalette::ButtonText), 1.8, Qt::SolidLine, Qt::RoundCap);
  p->drawLine(QPointF(rect.left() + 1, rect.center().y()),
              QPointF(rect.right() - 1, rect.center().y()));
}

void RDStyle::drawPrimitive(PrimitiveElement element, const QStyleOption *opt, QPainter *p,
                            const QWidget *widget) const
{
  if(element == QStyle::PE_PanelLineEdit)
  {
    const QStyleOptionFrame *frame = qstyleoption_cast<const QStyleOptionFrame *>(opt);

    if(frame && frame->lineWidth > 0)
    {
      QStyleOption o = *opt;
      o.state &= ~State_Sunken;
      drawRoundedRectBorder(&o, p, widget, QPalette::Base, false);
    }
    else
    {
      p->fillRect(opt->rect.adjusted(0, 0, -1, -1), opt->palette.brush(QPalette::Base));
    }

    return;
  }
  else if(element == QStyle::PE_Frame)
  {
    const QStyleOptionFrame *frame = qstyleoption_cast<const QStyleOptionFrame *>(opt);

    QStyleOptionFrame frameOpt = *frame;
    frameOpt.frameShape = QFrame::Panel;
    proxy()->drawControl(CE_ShapedFrame, &frameOpt, p, widget);
    return;
  }
  else if(element == QStyle::PE_FrameFocusRect)
  {
    // don't draw focus rects
    return;
  }
  else if(element == QStyle::PE_FrameStatusBarItem)
  {
    // don't draw any panel around status bar items
    return;
  }
  else if(element == QStyle::PE_PanelTipLabel)
  {
    QPen oldPen = p->pen();

    p->fillRect(opt->rect, opt->palette.brush(QPalette::ToolTipBase));

    p->setPen(QPen(m_Scheme == Ultra
                       ? QBrush(UltraColors::color(UltraColors::AppToolbarBackground))
                       : outlineBrush(opt->palette),
                   0));
    p->drawRect(opt->rect.adjusted(0, 0, -1, -1));

    p->setPen(oldPen);

    return;
  }
  else if(element == QStyle::PE_FrameMenu)
  {
    drawRoundedRectBorder(opt, p, widget, QPalette::NoRole, false);
    return;
  }
  else if(element == QStyle::PE_PanelMenu)
  {
    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::DropdownBackground));
    return;
  }
  else if(element == QStyle::PE_PanelMenuBar)
  {
    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::AppToolbarBackground));
    return;
  }
  else if(element == QStyle::PE_PanelToolBar && m_Scheme == Ultra)
  {
    p->fillRect(opt->rect, UltraColors::color(UltraColors::ToolbarBackground));
    p->setPen(QPen(UltraColors::color(UltraColors::DefaultBorder), 1.0));
    if(!(opt->state & State_Horizontal))
      p->drawLine(opt->rect.topRight(), opt->rect.bottomRight());
    else
      p->drawLine(opt->rect.bottomLeft(), opt->rect.bottomRight());
    return;
  }
  else if(element == QStyle::PE_FrameTabBarBase)
  {
    QPen oldPen = p->pen();
    p->setPen(QPen(outlineBrush(opt->palette), 0));
    p->drawLine(opt->rect.bottomLeft(), opt->rect.bottomRight());
    p->setPen(oldPen);
    return;
  }
  else if(element == QStyle::PE_FrameTabWidget)
  {
    const QStyleOptionTabWidgetFrame *tabwidget =
        qstyleoption_cast<const QStyleOptionTabWidgetFrame *>(opt);

    QRegion region;

    // include the whole rect, *except* the part just under the tabs. The border under them is drawn
    // as part of the tab itself so the selected tab can avoid it
    region += opt->rect;

    QRect topRect = opt->rect;
    topRect.adjust(1, 0, -1, 0);
    topRect.setHeight(2);

    region -= topRect;

    p->save();

    p->setClipRegion(region);

    QStyleOptionTabWidgetFrame border = *tabwidget;
    border.state &= ~State_HasFocus;
    drawRoundedRectBorder(&border, p, widget, QPalette::NoRole, false);

    p->restore();

    p->setPen(QPen(outlineBrush(opt->palette), 1.0));

    // draw vertical lines down from top left/right corners to straighten it.
    p->drawLine(opt->rect.topLeft(), opt->rect.topLeft() + QPoint(0, 1));
    p->drawLine(opt->rect.topRight(), opt->rect.topRight() + QPoint(0, 1));

    // draw a vertical line to complete the tab bottoms
    QRect tabBottomLine = opt->rect.adjusted(0, -1, 0, -opt->rect.height());
    p->drawLine(tabBottomLine.topLeft(), tabBottomLine.topRight());

    return;
  }
  else if(element == QStyle::PE_IndicatorViewItemCheck || element == QStyle::PE_IndicatorCheckBox)
  {
    QRect rect = opt->rect;
    int w = rect.width();
    int h = rect.height();
    if(w < h)
    {
      int padding = (h - w) / 2;
      rect.setHeight(w);
      rect.adjust(0, padding, 0, padding);
    }
    else if(h < w)
    {
      int padding = (w - h) / 2;
      rect.setWidth(h);
      rect.adjust(padding, 0, padding, 0);
    }

    QPen outlinePen(outlineBrush(opt->palette), 1.0);

    p->save();
    p->setClipRect(rect);
    p->setRenderHint(QPainter::Antialiasing);

    rect.adjust(0, 0, -1, -1);

    QPainterPath path;
    path.addRoundedRect(rect, 1.0, 1.0);

    p->setPen(outlinePen);
    p->drawPath(path.translated(QPointF(0.5, 0.5)));

    if(m_Scheme == Ultra && (opt->state & State_On))
      p->fillPath(path, opt->palette.brush(QPalette::Highlight));

    rect = rect.adjusted(2, 2, -1, -1);

    if(opt->state & State_On)
    {
      if(m_Scheme == Ultra)
        drawCheckmark(opt, p, rect);
      else
        drawX(opt, p, rect);
    }
    else if(opt->state & State_NoChange)
    {
      if(m_Scheme == Ultra)
        drawMixedMark(opt, p, rect);
      else
      {
        QBrush brush = opt->palette.brush(QPalette::ButtonText);
        brush.setTexture(m_PartialCheckPattern);
        p->fillRect(rect, brush);
      }
    }

    p->restore();

    return;
  }
  else if(element == PE_PanelItemViewItem)
  {
    const QStyleOptionViewItem *viewitem = qstyleoption_cast<const QStyleOptionViewItem *>(opt);

    QPalette::ColorGroup group = QPalette::Normal;

    if((widget && !widget->isEnabled()) || !(viewitem->state & QStyle::State_Enabled))
      group = QPalette::Disabled;
    else if(!(viewitem->state & QStyle::State_Active))
      group = QPalette::Inactive;

    if(viewitem->state & QStyle::State_Selected)
    {
      if(viewitem->backgroundBrush.style() != Qt::NoBrush)
      {
        p->fillRect(viewitem->rect, viewitem->backgroundBrush);
        // If we have a custom color, use the selection color at half opacity over the custom color
        QColor col = viewitem->palette.color(group, QPalette::Highlight);
        col.setAlphaF(0.5f);
        p->fillRect(viewitem->rect, QBrush(col));
      }
      else
      {
        p->fillRect(viewitem->rect, viewitem->palette.brush(group, QPalette::Highlight));
      }
    }
    else
    {
      if(viewitem->backgroundBrush.style() != Qt::NoBrush)
        p->fillRect(viewitem->rect, viewitem->backgroundBrush);

      if(m_Scheme == Ultra && (viewitem->state & QStyle::State_Enabled) &&
         (viewitem->state & QStyle::State_MouseOver))
        p->fillRect(viewitem->rect, QColor(255, 255, 255, 16));
    }

    return;
  }

  RDTweakedNativeStyle::drawPrimitive(element, opt, p, widget);
}

const QBrush &RDStyle::outlineBrush(const QPalette &pal, QPalette::ColorRole role) const
{
  if(role == QPalette::Text || role == QPalette::WindowText)
    return m_Scheme == Light ? pal.brush(QPalette::WindowText) : pal.brush(QPalette::Light);

  return pal.brush(role);
}

void RDStyle::drawControl(ControlElement control, const QStyleOption *opt, QPainter *p,
                          const QWidget *widget) const
{
  if(control == CE_PushButton)
  {
    drawRoundedRectBorder(opt, p, widget, QPalette::Button, true);

    QCommonStyle::drawControl(CE_PushButtonLabel, opt, p, widget);
    return;
  }
  else if(control == CE_PushButtonBevel)
  {
    drawRoundedRectBorder(opt, p, widget, QPalette::Button, true);
    return;
  }
  else if(control == CE_RadioButton)
  {
    const QStyleOptionButton *radiobutton = qstyleoption_cast<const QStyleOptionButton *>(opt);
    if(radiobutton)
    {
      QRectF rect = proxy()->subElementRect(SE_CheckBoxIndicator, opt, widget);

      rect = rect.adjusted(1.5, 1.5, -1, -1);

      p->save();
      p->setRenderHint(QPainter::Antialiasing);

      if(opt->state & State_HasFocus)
      {
        QPainterPath highlight;
        highlight.addEllipse(rect.center(), rect.width() / 2.0 + 1.25, rect.height() / 2.0 + 1.25);

        p->fillPath(highlight, opt->palette.brush(QPalette::Highlight));
      }

      QPainterPath path;
      path.addEllipse(rect.center(), rect.width() / 2.0, rect.height() / 2.0);

      p->fillPath(path, outlineBrush(opt->palette));

      rect = rect.adjusted(1, 1, -1, -1);

      path = QPainterPath();
      path.addEllipse(rect.center(), rect.width() / 2.0, rect.height() / 2.0);

      if(opt->state & State_Sunken)
        p->fillPath(path, opt->palette.brush(QPalette::Midlight));
      else
        p->fillPath(path, opt->palette.brush(QPalette::Button));

      if(opt->state & State_On)
      {
        rect = rect.adjusted(1.5, 1.5, -1.5, -1.5);

        path = QPainterPath();
        path.addEllipse(rect.center(), rect.width() / 2.0, rect.height() / 2.0);

        p->fillPath(path, opt->palette.brush(QPalette::ButtonText));
      }

      p->restore();

      QStyleOptionButton labelText = *radiobutton;
      labelText.rect = proxy()->subElementRect(SE_RadioButtonContents, &labelText, widget);
      drawControl(CE_RadioButtonLabel, &labelText, p, widget);
    }

    return;
  }
  else if(control == CE_CheckBox)
  {
    const QStyleOptionButton *checkbox = qstyleoption_cast<const QStyleOptionButton *>(opt);
    if(checkbox)
    {
      QRectF rect = proxy()->subElementRect(SE_CheckBoxIndicator, opt, widget).adjusted(1, 1, -1, -1);
      float w = rect.width();
      float h = rect.height();
      if(w < h)
      {
        int padding = (h - w) * 0.5;
        rect.setHeight(w);
        rect.adjust(0, padding, 0, padding);
      }
      else if(h < w)
      {
        int padding = (w - h) * 0.5;
        rect.setWidth(h);
        rect.adjust(padding, 0, padding, 0);
      }

      QPen outlinePen(outlineBrush(opt->palette), 1.0);

      p->save();
      p->setRenderHint(QPainter::Antialiasing);

      if(opt->state & State_HasFocus)
      {
        QPainterPath highlight;
        highlight.addRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), 1.0, 1.0);

        p->strokePath(highlight.translated(QPointF(0.5, 0.5)),
                      QPen(opt->palette.brush(QPalette::Highlight), 1.5));
      }

      QPainterPath path;
      path.addRoundedRect(rect, 1.0, 1.0);

      p->setPen(outlinePen);
      p->drawPath(path.translated(QPointF(0.5, 0.5)));

      if(m_Scheme == Ultra && (opt->state & State_On))
        p->fillPath(path, opt->palette.brush(QPalette::Highlight));

      rect = rect.adjusted(2, 2, -1, -1);

      if(opt->state & State_On)
      {
        if(m_Scheme == Ultra)
          drawCheckmark(opt, p, rect);
        else
          drawX(opt, p, rect);
      }
      else if(opt->state & State_NoChange)
      {
        if(m_Scheme == Ultra)
          drawMixedMark(opt, p, rect);
        else
        {
          QBrush brush = opt->palette.brush(QPalette::ButtonText);
          brush.setTexture(m_PartialCheckPattern);
          p->fillRect(rect, brush);
        }
      }

      p->restore();

      QStyleOptionButton labelText = *checkbox;
      labelText.rect = proxy()->subElementRect(SE_CheckBoxContents, &labelText, widget);
      proxy()->drawControl(CE_CheckBoxLabel, &labelText, p, widget);
    }

    return;
  }
  else if(control == CE_CheckBoxLabel || control == QStyle::CE_RadioButtonLabel)
  {
    const QStyleOptionButton *checkbox = qstyleoption_cast<const QStyleOptionButton *>(opt);
    if(checkbox)
    {
      QRect rect = checkbox->rect;

      if(!checkbox->icon.isNull())
      {
        proxy()->drawItemPixmap(
            p, rect, Qt::AlignLeft | Qt::AlignVCenter,
            checkbox->icon.pixmap(widgetWindow(widget), checkbox->iconSize,
                                  checkbox->state & State_Enabled ? QIcon::Normal : QIcon::Disabled));

        rect.setLeft(rect.left() + checkbox->iconSize.width() + Constants::CheckMargin);
      }

      if(!checkbox->text.isEmpty())
      {
        proxy()->drawItemText(p, rect, Qt::AlignLeft | Qt::AlignVCenter | Qt::TextHideMnemonic,
                              checkbox->palette, checkbox->state & State_Enabled, checkbox->text,
                              QPalette::WindowText);
      }
    }

    return;
  }
  else if(control == CE_SizeGrip)
  {
    // don't draw size grips
    return;
  }
  else if(control == CE_ShapedFrame)
  {
    const QStyleOptionFrame *frame = qstyleoption_cast<const QStyleOptionFrame *>(opt);

    qreal lineWidth = qMax(1, frame->lineWidth);

    p->save();

    p->setPen(QPen(outlineBrush(opt->palette, widget->foregroundRole()), lineWidth));

    qreal adjust = 0.5 * lineWidth;

    QRectF rect = QRectF(opt->rect).adjusted(adjust, adjust, -adjust, -adjust);

    QPainterPath path;
    path.addRoundedRect(rect, 1.0, 1.0);

    if(frame->frameShape == QFrame::NoFrame)
    {
      // draw nothing
    }
    else if(frame->frameShape == QFrame::Box)
    {
      p->drawRect(rect);
    }
    else if(frame->frameShape == QFrame::Panel || frame->frameShape == QFrame::WinPanel ||
            frame->frameShape == QFrame::StyledPanel)
    {
      p->setRenderHint(QPainter::Antialiasing);
      p->drawPath(path);
    }
    else if(frame->frameShape == QFrame::HLine)
    {
      rect.adjust(Constants::SeparatorMargin, 0, -Constants::SeparatorMargin, 0);
      QPoint offs(0, opt->rect.height() / 2);
      p->drawLine(opt->rect.topLeft() + offs, opt->rect.topRight() + offs);
    }
    else if(frame->frameShape == QFrame::VLine)
    {
      rect.adjust(0, Constants::SeparatorMargin, 0, -Constants::SeparatorMargin);
      QPoint offs(opt->rect.width() / 2, 0);
      p->drawLine(opt->rect.topLeft() + offs, opt->rect.bottomLeft() + offs);
    }

    p->restore();

    return;
  }
  else if(control == QStyle::CE_ProgressBar)
  {
    QRect rect = opt->rect;

    rect.adjust(Constants::ProgressMargin, Constants::ProgressMargin, -Constants::ProgressMargin,
                -Constants::ProgressMargin);

    QPainterPath path;
    const qreal radius = m_Scheme == Ultra ? 3.0 : Constants::ProgressRadius;
    path.addRoundedRect(rect, radius, radius);

    const QStyleOptionProgressBar *progress = qstyleoption_cast<const QStyleOptionProgressBar *>(opt);

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    p->setPen(QPen(outlineBrush(opt->palette), 1.0));
    p->drawPath(path);

    if(progress->minimum >= progress->maximum && opt->styleObject)
    {
      // animate an 'infinite' progress bar by adding animated clip regions
      if(!Animation::has(opt->styleObject))
        Animation::start(new RDProgressAnimation(2, 30, opt->styleObject));

      RDProgressAnimation *anim = Animation::get<RDProgressAnimation>(opt->styleObject);

      QRegion region;

      rect.setWidth(anim->chunkSize());

      rect.moveLeft(rect.left() + anim->offset());

      while(rect.intersects(opt->rect))
      {
        region += rect;

        // step two chunks, to skip over the chunk we're excluding from the region
        rect.moveLeft(rect.left() + anim->chunkSize() * 2);
      }

      p->setClipRegion(region);
    }

    // if we're rendering a normal progress bar, set the clip rect
    if(progress->minimum < progress->maximum)
    {
      qreal delta = qreal(progress->progress) / qreal(progress->maximum - progress->minimum);
      rect.setRight(rect.left() + rect.width() * delta);

      p->setClipRect(rect);
    }

    p->fillPath(path, opt->palette.brush(QPalette::Highlight));

    p->restore();

    return;
  }
  else if(control == QStyle::CE_ProgressBarGroove)
  {
    return;
  }
  else if(control == QStyle::CE_Splitter)
  {
    p->eraseRect(opt->rect);
    return;
  }
  else if(control == QStyle::CE_MenuBarEmptyArea)
  {
    QRect rect = opt->rect;
    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::AppToolbarBackground));
    else
      p->eraseRect(opt->rect);
    rect.adjust(0, -2, 0, -2);
    p->setPen(QPen(m_Scheme == Ultra
                       ? QBrush(UltraColors::color(UltraColors::DefaultBorder))
                       : outlineBrush(opt->palette),
                   1.0));
    p->drawLine(rect.bottomLeft(), rect.bottomRight());
    return;
  }
  else if(control == QStyle::CE_MenuBarItem)
  {
    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    QRectF rect = QRectF(opt->rect).adjusted(0.5, 0.5, 0.5, 0.5);

    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::AppToolbarBackground));

    const QStyleOptionMenuItem *menuitem = qstyleoption_cast<const QStyleOptionMenuItem *>(opt);

    p->setPen(QPen(outlineBrush(opt->palette), 1.0));

    QPalette::ColorRole textrole = QPalette::WindowText;

    QStyle::State mask = State_Enabled | State_Selected;
    if((opt->state & mask) == mask)
    {
      qreal radius = 2.0;

      if(opt->state & State_Sunken)
        radius = 1.0;

      QPainterPath path;
      path.addRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius);
      if(m_Scheme == Ultra)
      {
        p->fillPath(path, UltraColors::color((opt->state & State_Sunken)
                                                 ? UltraColors::AppToolbarBackgroundPressed
                                                 : UltraColors::AppToolbarBackgroundHover));
      }
      else
      {
        p->fillPath(path, opt->palette.brush(QPalette::Highlight));
      }

      if(m_Scheme != Ultra)
        textrole = QPalette::HighlightedText;

      if(opt->state & State_Sunken)
        p->drawPath(path);
    }

    rect.adjust(Constants::MenuBarMargin, 0, -Constants::MenuBarMargin, 0);

    if(!menuitem->icon.isNull())
    {
      int iconSize = proxy()->pixelMetric(QStyle::PM_SmallIconSize, opt, widget);

      QPixmap pix =
          menuitem->icon.pixmap(widgetWindow(widget), QSize(iconSize, iconSize),
                                (menuitem->state & State_Enabled) ? QIcon::Normal : QIcon::Disabled);

      if(!pix.isNull())
      {
        QRectF iconRect = rect;
        iconRect.setWidth(iconSize);
        proxy()->drawItemPixmap(p, iconRect.toRect(),
                                Qt::AlignCenter | Qt::AlignTop | Qt::TextShowMnemonic, pix);
        rect.adjust(iconSize + Constants::MenuBarMargin, 0, 0, 0);
      }
    }
    else if(menuitem->checkType != QStyleOptionMenuItem::NotCheckable)
    {
      int checkSize = proxy()->pixelMetric(QStyle::PM_SmallIconSize, opt, widget);

      QRectF checkRect = rect.adjusted(1, 1, -1, -1);
      checkRect.setWidth(checkSize);

      // vertically align the check
      int excess = checkRect.height() - checkRect.width();
      if(excess > 0)
      {
        checkRect.setHeight(checkRect.height() - excess / 2);
        excess -= excess / 2;
        checkRect.setTop(checkRect.top() + excess);
      }

      QStyleOptionButton box;
      (QStyleOption &)box = *(QStyleOption *)opt;
      box.rect = checkRect.toRect().adjusted(1, 1, -1, -1);
      if(menuitem->checked)
        box.state |= State_On;
      else
        box.state &= ~State_On;
      proxy()->drawPrimitive(PE_IndicatorCheckBox, &box, p, widget);

      rect.adjust(checkSize + Constants::MenuBarMargin, 0, 0, 0);
    }

    if(menuitem->menuItemType == QStyleOptionMenuItem::Normal)
    {
      p->setFont(menuitem->font);
      proxy()->drawItemText(p, rect.toRect(), Qt::AlignCenter | Qt::AlignTop | Qt::TextShowMnemonic,
                            menuitem->palette, menuitem->state & State_Enabled, menuitem->text,
                            textrole);
    }

    p->restore();

    return;
  }
  else if(control == QStyle::CE_MenuEmptyArea)
  {
    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::DropdownBackground));
    else
      p->eraseRect(opt->rect);
    return;
  }
  else if(control == QStyle::CE_MenuItem)
  {
    const QStyleOptionMenuItem *menuitem = qstyleoption_cast<const QStyleOptionMenuItem *>(opt);

    QPalette menuPalette = menuitem->palette;
    if(m_Scheme == Ultra)
    {
      const QColor dropdownText = UltraColors::color(UltraColors::DropdownText);
      menuPalette.setColor(QPalette::Active, QPalette::WindowText, dropdownText);
      menuPalette.setColor(QPalette::Inactive, QPalette::WindowText, dropdownText);
    }

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    QRectF rect = QRectF(opt->rect).adjusted(0.5, 0.5, 0.5, 0.5);

    if(m_Scheme == Ultra)
      p->fillRect(opt->rect, UltraColors::color(UltraColors::DropdownBackground));

    p->setPen(QPen(outlineBrush(opt->palette), 1.0));

    QPalette::ColorRole textrole = QPalette::WindowText;

    QStyle::State mask = State_Enabled | State_Selected;
    if((opt->state & mask) == mask)
    {
      qreal radius = 2.0;

      if(opt->state & State_Sunken)
        radius = 1.0;

      QPainterPath path;
      path.addRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius);
      if(m_Scheme == Ultra)
        p->fillPath(path, UltraColors::color(UltraColors::DropdownBackgroundHover));
      else
        p->fillPath(path, opt->palette.brush(QPalette::Highlight));

      if(m_Scheme != Ultra)
        textrole = QPalette::HighlightedText;

      if(opt->state & State_Sunken)
        p->drawPath(path);
    }

    rect.adjust(Constants::MenuBarMargin, 0, -Constants::MenuBarMargin, 0);

    if(menuitem->menuItemType == QStyleOptionMenuItem::Separator)
    {
      QPointF left = rect.center();
      QPointF right = rect.center();

      left.setX(rect.left());
      right.setX(rect.right());

      p->drawLine(left, right);
    }

    // draw the icon, if it exists
    if(!menuitem->icon.isNull())
    {
      proxy()->drawItemPixmap(
          p, rect.toRect(), Qt::AlignLeft | Qt::AlignVCenter,
          menuitem->icon.pixmap(widgetWindow(widget),
                                QSize(Constants::MenuBarIconSize, Constants::MenuBarIconSize),
                                menuitem->state & State_Enabled ? QIcon::Normal : QIcon::Disabled));
    }
    else if(menuitem->checkType != QStyleOptionMenuItem::NotCheckable)
    {
      QRectF checkRect = rect.adjusted(1, 1, -1, -1);
      checkRect.setWidth(Constants::MenuBarIconSize);

      // vertically align the check
      int excess = checkRect.height() - checkRect.width();
      if(excess > 0)
      {
        checkRect.setHeight(checkRect.height() - excess / 2);
        excess -= excess / 2;
        checkRect.setTop(checkRect.top() + excess);
      }

      QStyleOptionButton box;
      (QStyleOption &)box = *(QStyleOption *)opt;
      box.rect = checkRect.toRect().adjusted(1, 1, -1, -1);
      if(menuitem->checked)
        box.state |= State_On;
      else
        box.state &= ~State_On;
      proxy()->drawPrimitive(PE_IndicatorCheckBox, &box, p, widget);
    }

    if(menuitem->menuHasCheckableItems)
      rect.adjust(
          Constants::MenuBarMargin + std::max(Constants::MenuBarIconSize, menuitem->maxIconWidth),
          0, 0, 0);
    else if(menuitem->maxIconWidth)
      rect.adjust(Constants::MenuBarMargin + menuitem->maxIconWidth, 0, 0, 0);

    if(menuitem->menuItemType == QStyleOptionMenuItem::Normal ||
       menuitem->menuItemType == QStyleOptionMenuItem::SubMenu)
    {
      p->setFont(menuitem->font);

      QString text = menuitem->text;

      int tabIndex = text.indexOf(QLatin1Char('\t'));

      if(tabIndex < 0)
      {
        proxy()->drawItemText(
            p, rect.toRect(), Qt::AlignLeft | Qt::AlignVCenter | Qt::TextShowMnemonic,
            menuPalette, menuitem->state & State_Enabled, menuitem->text, textrole);
      }
      else
      {
        QString title = text.left(tabIndex);
        QString shortcut = text.mid(tabIndex + 1, -1);

        proxy()->drawItemText(p, rect.toRect(),
                              Qt::AlignLeft | Qt::AlignVCenter | Qt::TextShowMnemonic,
                              menuPalette, menuitem->state & State_Enabled, title, textrole);
        proxy()->drawItemText(
            p, rect.toRect(), Qt::AlignRight | Qt::AlignVCenter | Qt::TextShowMnemonic,
            menuPalette, menuitem->state & State_Enabled, shortcut, textrole);
      }

      if(menuitem->menuItemType == QStyleOptionMenuItem::SubMenu)
      {
        QStyleOptionMenuItem submenu = *menuitem;
        submenu.rect.setLeft(submenu.rect.right() - Constants::MenuSubmenuWidth);
        proxy()->drawPrimitive(PE_IndicatorArrowRight, &submenu, p, widget);
      }
    }

    p->restore();

    return;
  }
  else if(control == QStyle::CE_TabBarTabLabel)
  {
    const QStyleOptionTab *tab = qstyleoption_cast<const QStyleOptionTab *>(opt);

    QRect rect = tab->rect;

    rect.adjust(Constants::TabMargin, 0, 0, 0);

    if(!tab->icon.isNull())
    {
      proxy()->drawItemPixmap(
          p, rect, Qt::AlignLeft | Qt::AlignVCenter,
          tab->icon.pixmap(widgetWindow(widget), tab->iconSize,
                           tab->state & State_Enabled ? QIcon::Normal : QIcon::Disabled));

      rect.setLeft(rect.left() + tab->iconSize.width() + Constants::TabMargin);
    }

    if(m_Scheme == Ultra)
    {
      QPalette palette = tab->palette;
      const QColor tabText = UltraColors::color((tab->state & State_Selected)
                                                     ? UltraColors::ButtonText
                                                     : UltraColors::TabText);
      palette.setColor(QPalette::Active, QPalette::WindowText, tabText);
      palette.setColor(QPalette::Inactive, QPalette::WindowText, tabText);
      proxy()->drawItemText(p, rect, Qt::AlignLeft | Qt::AlignVCenter | Qt::TextHideMnemonic,
                            palette, tab->state & State_Enabled, tab->text, QPalette::WindowText);
    }
    else
    {
      proxy()->drawItemText(p, rect, Qt::AlignLeft | Qt::AlignVCenter | Qt::TextHideMnemonic,
                            tab->palette, tab->state & State_Enabled, tab->text,
                            QPalette::WindowText);
    }
    return;
  }
  else if(control == QStyle::CE_TabBarTabShape)
  {
    const QStyleOptionTab *tab = qstyleoption_cast<const QStyleOptionTab *>(opt);

    QRect rect = opt->rect;

    rect.adjust(0, 0, 0, 100);

    if(tab->position == QStyleOptionTab::OnlyOneTab || tab->position == QStyleOptionTab::End ||
       (opt->state & State_Selected))
      rect.setRight(rect.right() - 1);

    if(tab->selectedPosition == QStyleOptionTab::PreviousIsSelected)
      rect.setLeft(rect.left() - 1);

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    p->setPen(QPen(m_Scheme == Ultra
                       ? QBrush(UltraColors::color(UltraColors::DefaultBorder))
                       : outlineBrush(opt->palette),
                   0.0));

    QPainterPath path;
    path.addRoundedRect(rect, 3.0, 3.0);

    if(m_Scheme == Ultra)
    {
      // i18n-ignore: internal QObject property identifier, never shown to the user.
      const bool brightSelectedTabs =
          widget && widget->property("_renderdoc_ultra_bright_selected_tabs").toBool();

      if(opt->state & State_Selected)
        p->fillPath(path, UltraColors::color(brightSelectedTabs
                                                ? UltraColors::WindowBackground
                                                : UltraColors::TabBackgroundSelected));
      else if(opt->state & State_MouseOver)
        p->fillPath(path, UltraColors::color(brightSelectedTabs
                                                ? UltraColors::HeaderBackgroundHover
                                                : UltraColors::TabBackgroundHover));
      else
        p->fillPath(path, UltraColors::color(UltraColors::TabBackground));
    }
    else
    {
      if(opt->state & State_Selected)
        p->fillPath(path, opt->palette.brush(QPalette::Window));
      else if(opt->state & State_MouseOver)
        p->fillPath(path, opt->palette.brush(QPalette::Midlight));
      else
        p->fillPath(path, opt->palette.brush(QPalette::Disabled, QPalette::Window));
    }

    p->drawPath(path.translated(QPointF(0.5, 0.5)));

    if(!(opt->state & State_Selected))
    {
      QRectF bottomLine = QRectF(opt->rect).adjusted(0, -0.5, 0, 0);
      p->drawLine(bottomLine.bottomLeft(), bottomLine.bottomRight());
    }

    p->restore();
    return;
  }
  else if(control == QStyle::CE_TabBarTab)
  {
    proxy()->drawControl(CE_TabBarTabShape, opt, p, widget);
    proxy()->drawControl(CE_TabBarTabLabel, opt, p, widget);
    return;
  }
  else if(control == QStyle::CE_DockWidgetTitle)
  {
    QColor mid = opt->palette.color(QPalette::Mid);
    QColor window = opt->palette.color(QPalette::Window);

    QColor backGround;
    if(m_Scheme == Ultra)
    {
      backGround = UltraColors::color(UltraColors::DockTitleBackground);
    }
    else
    {
      backGround = QColor::fromRgbF(0.5f * mid.redF() + 0.5f * window.redF(),
                                    0.5f * mid.greenF() + 0.5f * window.greenF(),
                                    0.5f * mid.blueF() + 0.5f * window.blueF());
    }

    QRectF rect = QRectF(opt->rect).adjusted(0.5, 0.5, 0.0, 0.0);

    p->fillRect(rect, backGround);

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    p->setPen(QPen(m_Scheme == Ultra
                       ? QBrush(UltraColors::color(UltraColors::WindowBorder))
                       : outlineBrush(opt->palette),
                   1.0));

    QPainterPath path;
    path.addRoundedRect(rect, 1.0, 1.0);

    p->drawPath(path);

    p->restore();

    const QStyleOptionDockWidget *dockwidget = qstyleoption_cast<const QStyleOptionDockWidget *>(opt);

    proxy()->drawItemText(p, rect.toRect().adjusted(Constants::TabMargin, 0, 0, 0),
                          Qt::AlignLeft | Qt::AlignTop | Qt::TextHideMnemonic, dockwidget->palette,
                          dockwidget->state & State_Enabled, dockwidget->title, QPalette::WindowText);

    return;
  }
  else if(control == QStyle::CE_Header)
  {
    const QStyleOptionHeader *header = qstyleoption_cast<const QStyleOptionHeader *>(opt);

    QRectF rect = QRectF(opt->rect).adjusted(0.0, 0.0, -0.5, -0.5);

    p->save();

    p->setPen(QPen(m_Scheme == Ultra
                       ? QBrush(UltraColors::color(UltraColors::DefaultBorder))
                       : outlineBrush(opt->palette),
                   1.0));

    if(m_Scheme == Ultra)
    {
      QRgb background = UltraColors::ToolbarBackground;
      if(opt->state & State_Sunken)
        background = UltraColors::HeaderBackgroundPressed;
      else if(opt->state & State_MouseOver)
        background = UltraColors::HeaderBackgroundHover;
      p->fillRect(rect, UltraColors::color(background));
    }
    else
    {
      p->fillRect(rect, opt->palette.brush(QPalette::Midlight));
    }
    p->drawLine(rect.bottomLeft(), rect.bottomRight());
    p->drawLine(rect.topRight(), rect.bottomRight());

    rect.adjust(Constants::ItemHeaderMargin, 0, -Constants::ItemHeaderMargin, 0);

    // draw the icon, if it exists
    if(!header->icon.isNull())
    {
      proxy()->drawItemPixmap(
          p, rect.toRect(), Qt::AlignLeft | Qt::AlignVCenter,
          header->icon.pixmap(widgetWindow(widget),
                              QSize(Constants::ItemHeaderIconSize, Constants::ItemHeaderIconSize),
                              header->state & State_Enabled ? QIcon::Normal : QIcon::Disabled));
    }

    proxy()->drawItemText(p, rect.toRect(), Qt::AlignLeft | Qt::AlignVCenter | Qt::TextHideMnemonic,
                          header->palette, header->state & State_Enabled, header->text,
                          QPalette::WindowText);

    if(header->sortIndicator != QStyleOptionHeader::None)
    {
      p->setRenderHint(QPainter::Antialiasing);

      qreal penWidth = 1.5;
      p->setPen(QPen(opt->palette.brush(QPalette::WindowText), penWidth));

      {
        QRectF arrowRect = rect;
        arrowRect.setLeft(arrowRect.right() - Constants::SpinButtonDim);

        qreal yoffset = 2.5f;
        if(header->sortIndicator == QStyleOptionHeader::SortUp)
          yoffset = -yoffset;

        qreal ycentre = arrowRect.center().y();

        QPainterPath path;
        QPolygonF poly;

        QPointF pt;
        pt.setX(arrowRect.left() + penWidth);
        pt.setY(ycentre + yoffset);
        poly << pt;

        pt.setX(arrowRect.center().x());
        pt.setY(ycentre - yoffset);
        poly << pt;

        pt.setX(arrowRect.right() - penWidth);
        pt.setY(ycentre + yoffset);
        poly << pt;

        path.addPolygon(poly);

        p->drawPath(path);
      }
    }

    p->restore();

    return;
  }

  RDTweakedNativeStyle::drawControl(control, opt, p, widget);
}

void RDStyle::drawRoundedRectBorder(const QStyleOption *opt, QPainter *p, const QWidget *widget,
                                    QPalette::ColorRole fillRole, bool shadow) const
{
  if(m_Scheme == Ultra)
  {
    QColor fill = fillRole == QPalette::NoRole ? QColor() : opt->palette.color(fillRole);
    QColor border = UltraColors::color(UltraColors::WindowBorder);
    QColor accent = UltraColors::color(UltraColors::AccentBorder);

    const bool enabled = opt->state & State_Enabled;
    const bool hovered = opt->state & State_MouseOver;
    const bool pressed = opt->state & State_Sunken;
    const bool focused = opt->state & State_HasFocus;
    const bool checked = opt->state & State_On;
    const bool autoRaise = opt->state & State_AutoRaise;
    const bool comboBox = qobject_cast<const QComboBox *>(widget) != NULL;

    if(fillRole == QPalette::Button)
    {
      if(autoRaise)
      {
        fill = UltraColors::color(UltraColors::ToolbarBackground);
        border = fill;
        if(enabled && (pressed || checked))
        {
          fill = UltraColors::color(UltraColors::HeaderBackgroundPressed);
          border = UltraColors::color(UltraColors::DefaultBorder);
        }
        else if(enabled && (hovered || focused))
        {
          fill = UltraColors::color(UltraColors::HeaderBackgroundHover);
          border = UltraColors::color(UltraColors::DefaultBorder);
        }
      }
      else if(!enabled)
      {
        fill = UltraColors::color(UltraColors::ButtonDisabled);
        border = UltraColors::color(UltraColors::ButtonBorder);
      }
      else if(pressed || checked)
      {
        fill = UltraColors::color(hovered ? UltraColors::ButtonBackgroundHoverPressed
                                         : UltraColors::ButtonBackgroundPressed);
        border = UltraColors::color(UltraColors::DeepBorder);
        accent = UltraColors::color(UltraColors::DeepBorder);
      }
      else if(hovered)
      {
        fill = UltraColors::color(UltraColors::ButtonBackgroundHover);
        border = UltraColors::color(UltraColors::ButtonBorder);
      }
      else if(focused)
      {
        fill = UltraColors::color(UltraColors::ButtonBackgroundFocus);
        border = UltraColors::color(UltraColors::ButtonFocusBorder);
      }
      else
      {
        fill = UltraColors::color(UltraColors::ButtonBackground);
        border = UltraColors::color(UltraColors::ButtonBorder);
      }

      if(focused && enabled && !autoRaise)
        border = UltraColors::color(UltraColors::ButtonFocusBorder);
    }
    else if(fillRole == QPalette::Base)
    {
      if(comboBox)
      {
        fill = UltraColors::color(!enabled ? UltraColors::DefaultBackground
                                           : hovered ? UltraColors::DropdownBackgroundHover
                                                     : UltraColors::DropdownBackground);
        border = UltraColors::color(focused ? UltraColors::FocusBorder
                                           : UltraColors::ButtonBorder);
      }
      else
      {
        fill = UltraColors::color(enabled ? UltraColors::InputBackground
                                          : UltraColors::DefaultBackground);
        border = UltraColors::color(enabled && focused
                                        ? UltraColors::FocusBorder
                                        : enabled && hovered ? UltraColors::HoverBorder
                                                             : UltraColors::InputBorder);
        accent = UltraColors::color(UltraColors::DeepBorder);
      }
    }
    else if(fillRole == QPalette::Window)
    {
      fill = UltraColors::color(UltraColors::WindowBackground);
      border = UltraColors::color(UltraColors::WindowBorder);
    }

    p->save();
    p->setRenderHint(QPainter::Antialiasing);

    QRectF rect = QRectF(opt->rect).adjusted(0.5, 0.5, -0.5, -0.5);
    QPainterPath path;
    path.addRoundedRect(rect, 3.0, 3.0);

    if(fillRole != QPalette::NoRole)
      p->fillPath(path, fill);

    p->setPen(QPen(border, 1.0));
    p->drawPath(path);

    // Unity uses a flat accent edge instead of a drop shadow to separate stacked controls.
    if(fillRole == QPalette::Button && !autoRaise && enabled)
    {
      p->setPen(QPen(accent, 1.0));
      p->drawLine(rect.bottomLeft() + QPointF(3.0, 0.0),
                  rect.bottomRight() - QPointF(3.0, 0.0));
    }
    else if(fillRole == QPalette::Base && !comboBox && !focused)
    {
      p->setPen(QPen(accent, 1.0));
      p->drawLine(rect.topLeft() + QPointF(3.0, 0.0),
                  rect.topRight() - QPointF(3.0, 0.0));
    }

    p->restore();
    return;
  }

  QPen outlinePen(outlineBrush(opt->palette), 1.0);

  if(opt->state & State_HasFocus)
    outlinePen = QPen(opt->palette.brush(QPalette::Highlight), 1.5);

  p->save();

  p->setRenderHint(QPainter::Antialiasing);

  int xshift = proxy()->pixelMetric(PM_ButtonShiftHorizontal, opt, widget);
  int yshift = proxy()->pixelMetric(PM_ButtonShiftVertical, opt, widget);

  QRect rect = opt->rect.adjusted(0, 0, -1, -1);

  if(opt->state & State_Sunken)
  {
    rect.setLeft(rect.left() + xshift);
    rect.setTop(rect.top() + yshift);

    QPainterPath path;
    path.addRoundedRect(rect, 1.0, 1.0);

    p->fillPath(path, opt->palette.brush(QPalette::Midlight));

    p->setPen(outlinePen);
    p->drawPath(path.translated(QPointF(0.5, 0.5)));
  }
  else
  {
    if(shadow)
    {
      rect.setRight(rect.right() - xshift);
      rect.setBottom(rect.bottom() - yshift);
    }

    QPainterPath path;
    path.addRoundedRect(rect, 1.0, 1.0);

    if(shadow)
    {
      p->setPen(QPen(opt->palette.brush(QPalette::Shadow), 1.0));
      p->drawPath(path.translated(QPointF(1.0, 1.0)));
    }

    p->fillPath(path, opt->palette.brush(fillRole));

    p->setPen(outlinePen);
    p->drawPath(path.translated(QPointF(0.5, 0.5)));
  }

  p->restore();
}

RDProgressAnimation::RDProgressAnimation(int stepSize, int chunkSize, QObject *parent)
    : QAbstractAnimation(parent)
{
  m_stepSize = stepSize;
  m_offset = 0;
  m_chunkSize = chunkSize;
  m_prevTime = 0;
}

void RDProgressAnimation::updateCurrentTime(int currentTime)
{
  // update every 33ms, for a 30Hz animation
  const int rate = 33;

  // how many steps to take
  int steps = 0;

  int delta = currentTime - m_prevTime;

  // depending on how fast we're updated, we might have to process multiple frames together.
  while(delta > rate)
  {
    m_prevTime += rate;
    delta = currentTime - m_prevTime;

    steps++;
  }

  if(steps > 0)
  {
    m_offset += steps * m_stepSize;

    // the animation loops after two chunks, but to visualise a smooth animation with a new chunk
    // coming in from the left, we wrap to negative.
    // Consider the graph y = (x+1) % 2 - 1

    if(m_offset > m_chunkSize)
      m_offset -= m_chunkSize * 2;

    QEvent event(QEvent::StyleAnimationUpdate);
    event.setAccepted(false);
    QCoreApplication::sendEvent(parent(), &event);
  }
}
