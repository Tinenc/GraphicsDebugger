/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
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

#include "ThumbnailStrip.h"
#include <QScrollBar>
#include <QTimer>
#include "Widgets/ResourcePreview.h"
#include "ui_ThumbnailStrip.h"

ThumbnailStrip::ThumbnailStrip(QWidget *parent) : QWidget(parent), ui(new Ui::ThumbnailStrip)
{
  ui->setupUi(this);

  layout = new QVBoxLayout(ui->scrollAreaWidgetContents);
  layout->setSpacing(6);
  layout->setContentsMargins(6, 6, 6, 6);
  layout->setAlignment(Qt::AlignTop);
  ui->scrollArea->viewport()->setAutoFillBackground(true);
  ui->scrollAreaWidgetContents->setAutoFillBackground(true);
  const auto scrolled = [this]() {
    if(m_ScrollRefreshQueued)
      return;
    m_ScrollRefreshQueued = true;
    QTimer::singleShot(0, this, [this]() {
      m_ScrollRefreshQueued = false;
      // Native GPU children are not part of Qt's scroll blit. Repaint exposed labels/background
      // and ask visible children to re-present; preserve the cached resource data and layout.
      ui->scrollArea->viewport()->update();
      ui->scrollAreaWidgetContents->update();
      for(ResourcePreview *preview : m_Thumbnails)
        if(preview->isActive() && preview->isVisible() && !preview->visibleRegion().isEmpty())
          preview->RefreshNativeWindow();
    });
  };
  QObject::connect(ui->scrollArea->horizontalScrollBar(), &QScrollBar::valueChanged, this, scrolled);
  QObject::connect(ui->scrollArea->verticalScrollBar(), &QScrollBar::valueChanged, this, scrolled);
}

ThumbnailStrip::~ThumbnailStrip()
{
  delete layout;
  delete ui;
}

void ThumbnailStrip::addThumb(ResourcePreview *prev)
{
  layout->addWidget(prev);
  m_Thumbnails.push_back(prev);
}

void ThumbnailStrip::clearThumbs()
{
  m_LastActiveThumbnails.clear();
  m_LastLayoutSize = QSize();
  m_LastThumbnailCount = -1;
  for(ResourcePreview *p : m_Thumbnails)
  {
    layout->removeWidget(p);
    delete p;
  }

  m_Thumbnails.clear();
}

void ThumbnailStrip::resizeEvent(QResizeEvent *event)
{
  refreshLayout();
}

void ThumbnailStrip::mousePressEvent(QMouseEvent *event)
{
  emit(mouseClick(event));
}

void ThumbnailStrip::showEvent(QShowEvent *event)
{
  refreshLayout();
}

void ThumbnailStrip::refreshLayout()
{
  if(!isVisible())
  {
    // Binding metadata can change while this strip is hidden. Lay it out once when shown.
    m_LastLayoutSize = QSize();
    setUpdatesEnabled(true);
    return;
  }
  QVector<ResourcePreview *> active;
  for(ResourcePreview *preview : m_Thumbnails)
    if(preview->isActive())
      active.push_back(preview);
  if(m_LastLayoutSize == size() && m_LastThumbnailCount == m_Thumbnails.size() &&
     m_LastActiveThumbnails == active)
  {
    // TextureViewer retires and rebinds previews on every event. The final bindings can match
    // the previous layout while their widgets were hidden in between. Reuse geometry, not
    // transient visibility; otherwise native child surfaces and labels retain stale pixels.
    for(ResourcePreview *preview : m_Thumbnails)
      preview->setVisible(preview->isActive());
    setUpdatesEnabled(true);
    return;
  }
  m_LastLayoutSize = size();
  m_LastThumbnailCount = m_Thumbnails.size();
  m_LastActiveThumbnails = active;
  if(updatesEnabled())
    setUpdatesEnabled(false);
  for(ResourcePreview *preview : m_Thumbnails)
    if(!preview->isActive())
      preview->hide();

  QRect avail = geometry();
  avail.adjust(6, 6, -6, -6);

  int numActive = 0;
  for(ResourcePreview *c : m_Thumbnails)
    if(c->isActive())
      numActive++;

  // depending on overall aspect ratio, we either lay out the strip horizontally or
  // vertically. This tries to account for whether the strip is docked along one side
  // or another of the texture viewer
  if(avail.width() > avail.height())
  {
    avail.setWidth(avail.width() + 6);    // controls implicitly have a 6 margin on the right

    int aspectWidth = (int)((float)avail.height() * 1.3f);

    ui->scrollArea->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    delete layout;
    layout = new QHBoxLayout();
    layout->setSpacing(6);
    layout->setContentsMargins(6, 6, 6, 6);
    layout->setAlignment(Qt::AlignTop);

    int noscrollWidth = numActive * (aspectWidth + 20);

    if(noscrollWidth <= avail.width())
    {
      ui->scrollArea->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

      for(ResourcePreview *c : m_Thumbnails)
        c->setSize(QSize(aspectWidth, avail.height()));
    }
    else
    {
      ui->scrollArea->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOn);

      QScrollBar *hs = ui->scrollArea->horizontalScrollBar();

      avail.setHeight(qMax(1, avail.height() - hs->geometry().height()));

      aspectWidth = qMax(1, (int)((float)avail.height() * 1.3f));

      int totalWidth = numActive * (aspectWidth + 20);
      hs->setEnabled(totalWidth > avail.width());

      for(ResourcePreview *c : m_Thumbnails)
        c->setSize(QSize(aspectWidth, avail.height()));
    }
  }
  else
  {
    avail.setHeight(avail.height() + 6);    // controls implicitly have a 6 margin on the bottom

    int aspectHeight = (int)((float)avail.width() / 1.3f);

    ui->scrollArea->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

    delete layout;
    layout = new QVBoxLayout();
    layout->setSpacing(6);
    layout->setContentsMargins(6, 6, 6, 6);
    layout->setAlignment(Qt::AlignTop);

    int noscrollHeight = numActive * (aspectHeight + 6);

    if(noscrollHeight <= avail.height())
    {
      ui->scrollArea->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);

      for(ResourcePreview *c : m_Thumbnails)
        c->setSize(QSize(avail.width(), aspectHeight));
    }
    else
    {
      ui->scrollArea->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOn);

      QScrollBar *vs = ui->scrollArea->verticalScrollBar();

      avail.setWidth(qMax(1, avail.width() - vs->geometry().width()));

      aspectHeight = qMax(1, (int)((float)avail.width() / 1.3f));

      int totalHeight = numActive * (aspectHeight + 6);
      vs->setEnabled(totalHeight > avail.height());

      for(ResourcePreview *c : m_Thumbnails)
        c->setSize(QSize(avail.width(), aspectHeight));
    }
  }

  for(ResourcePreview *w : m_Thumbnails)
    layout->addWidget(w);

  for(ResourcePreview *w : m_Thumbnails)
    if(w->isActive())
      w->show();

  ui->scrollAreaWidgetContents->setLayout(layout);

  setUpdatesEnabled(true);
}

#if ENABLE_UNIT_TESTS
#include <QApplication>
#include <memory>
#include "3rdparty/catch/catch.hpp"
#include "Code/Interface/PersistentConfig.h"
#include "Code/QRDUtils.h"
TEST_CASE("Thumbnail layout reuse restores rebound previews", "[thumbnail-layout]")
{
  int argc = 1;
  char name[] = "thumbnail-layout-test";
  char *argv[] = {name, NULL};
  std::unique_ptr<QApplication> application;
  if(!QCoreApplication::instance())
    application.reset(new QApplication(argc, argv));
  Formatter::setParams(PersistentConfig());
  ThumbnailStrip strip;
  strip.setAttribute(Qt::WA_DontShowOnScreen);
  strip.resize(360, 640);
  ResourcePreview *preview = new ResourcePreview(true, &strip);
  strip.addThumb(preview);
  preview->setActive(true);
  strip.show();
  strip.refreshLayout();
  REQUIRE(preview->isVisible());
  preview->setActive(false);
  preview->setActive(true);
  // A binding transaction that returns to the same active set keeps native children visible.
  REQUIRE(preview->isVisible());
  strip.refreshLayout();
  CHECK(preview->isVisible());
  CHECK(preview->width() > 0);
  CHECK(preview->height() > 0);
  preview->setActive(false);
  strip.refreshLayout();
  CHECK(preview->isHidden());
}
#endif
