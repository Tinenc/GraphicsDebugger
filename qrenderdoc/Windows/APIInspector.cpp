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

#include "APIInspector.h"
#include <QSignalBlocker>
#include <QTimer>
#include "ui_APIInspector.h"

APIInspector::APIInspector(ICaptureContext &ctx, QWidget *parent)
    : QFrame(parent), ui(new Ui::APIInspector), m_Ctx(ctx)
{
  ui->setupUi(this);

  ui->apiEvents->setColumns({lit("EID"), tr("Event")});
  ui->apiEvents->header()->resizeSection(0, 150);

  ui->splitter->setCollapsible(1, true);
  ui->splitter->setSizes({1, 0});

  ui->callstack->setFont(Formatter::PreferredFont());
  ui->apiEvents->setFont(Formatter::PreferredFont());

  RDSplitterHandle *handle = (RDSplitterHandle *)ui->splitter->handle(1);
  handle->setTitle(tr("Callstack"));
  handle->setIndex(1);
  handle->setCollapsed(true);

  m_Ctx.AddCaptureViewer(this);
}

APIInspector::~APIInspector()
{
  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
  delete ui;
}

void APIInspector::RevealParameter(SDObject *param)
{
  if(!param)
    return;

  // An explicit parameter reveal needs its tree even if the tab is being raised afterwards.
  if(m_HasDeferredEvent && m_Ctx.IsCaptureLoaded())
    ApplySelectedEvent(m_Ctx.CurSelectedEvent());

  rdcarray<SDObject *> hierarchy;
  while(param)
  {
    hierarchy.push_back(param);
    param = param->GetParent();
  }

  if(hierarchy.back()->type.basetype == SDBasic::Chunk)
  {
    SDChunk *chunk = (SDChunk *)hierarchy.back();
    hierarchy.pop_back();

    int rootIdx = m_Chunks.indexOf(chunk);

    SDObject *current = chunk;

    if(rootIdx >= 0)
    {
      RDTreeWidgetItem *parent = ui->apiEvents->topLevelItem(rootIdx);

      while(parent)
      {
        ui->apiEvents->expandItem(parent);

        SDObject *next = hierarchy.back();
        hierarchy.pop_back();

        RDTreeWidgetItem *item = NULL;

        for(size_t i = 0; i < current->NumChildren(); i++)
        {
          if(current->GetChild(i) == next)
          {
            current = next;

            int idx = (int)i;

            // revealing a parameter that wasn't added, bail out now
            if(idx >= parent->childCount() || parent->child(idx)->tag().value<void *>() != next)
              return;

            item = parent->child(idx);
            break;
          }
        }

        parent = item;

        if(hierarchy.empty())
          break;
      }

      ui->apiEvents->setSelectedItem(parent);
      ui->apiEvents->scrollToItem(parent);
    }
  }
}

void APIInspector::OnCaptureLoaded()
{
  OnSelectedEventChanged(m_Ctx.CurSelectedEvent());
}

void APIInspector::OnCaptureClosed()
{
  m_Chunks.clear();
  ui->apiEvents->clear();
  ui->callstack->clear();
  ui->apiEvents->clearInternalExpansions();
  m_EventID = 0;
  m_HasDeferredEvent = false;
  m_EventInitialised = false;
}

void APIInspector::OnSelectedEventChanged(uint32_t eventId)
{
  // The first action can own thousands of setup API chunks. Building its recursive Qt tree
  // also expands lazy structured data, so a hidden inspector must not do this on navigation.
  if(!isVisible())
  {
    m_HasDeferredEvent = true;
    return;
  }
  ApplySelectedEvent(eventId);
}

void APIInspector::showEvent(QShowEvent *event)
{
  QFrame::showEvent(event);
  if(m_HasDeferredEvent)
    QTimer::singleShot(0, this, [this]() {
      if(isVisible() && m_HasDeferredEvent && m_Ctx.IsCaptureLoaded())
        ApplySelectedEvent(m_Ctx.CurSelectedEvent());
    });
}

void APIInspector::Refresh()
{
  if(m_HasDeferredEvent && m_Ctx.IsCaptureLoaded())
    ApplySelectedEvent(m_Ctx.CurSelectedEvent());
  on_apiEvents_itemSelectionChanged();
}

void APIInspector::ApplySelectedEvent(uint32_t eventId)
{
  m_HasDeferredEvent = false;
  // API chunks describe the immutable capture, independent of Shader or CBuffer replacements.
  if(m_EventInitialised && m_EventID == eventId)
    return;
  ui->apiEvents->saveExpansion(ui->apiEvents->getInternalExpansion(m_EventID), 0);

  ui->apiEvents->clearSelection();

  fillAPIView();

  m_EventID = eventId;
  m_EventInitialised = true;
  ui->apiEvents->applyExpansion(ui->apiEvents->getInternalExpansion(m_EventID), 0);
  // i18n-ignore: internal regression evidence; these properties are never displayed.
  setProperty("apiDisplayedEvent", eventId);
  // i18n-ignore: internal regression evidence, not a UI counter.
  setProperty("apiPopulateCount", qulonglong(++m_PopulateCount));
}

void APIInspector::addCallstack(rdcarray<rdcstr> calls)
{
  ui->callstack->setUpdatesEnabled(false);
  ui->callstack->clear();

  if(calls.size() == 1 && calls[0].isEmpty())
  {
    ui->callstack->addItem(tr("Symbols not loaded. Tools -> Resolve Symbols."));
  }
  else
  {
    for(rdcstr &s : calls)
      ui->callstack->addItem(s);
  }
  ui->callstack->setUpdatesEnabled(true);
}

void APIInspector::on_apiEvents_itemSelectionChanged()
{
  RDTreeWidgetItem *node = ui->apiEvents->selectedItem();

  SDChunk *chunk = NULL;
  // search up the tree to find the next parent with a chunk tag
  while(node)
  {
    chunk = (SDChunk *)node->tag().value<quintptr>();

    // if we found one, break
    if(chunk)
      break;

    // move to the parent
    node = node->parent();
  }

  if(chunk && !chunk->metadata.callstack.isEmpty())
  {
    if(m_Ctx.Replay().GetCaptureAccess())
    {
      m_Ctx.Replay().AsyncInvoke([this, chunk](IReplayController *) {
        rdcarray<rdcstr> stack =
            m_Ctx.Replay().GetCaptureAccess()->GetResolve(chunk->metadata.callstack);

        GUIInvoke::call(this, [this, stack]() { addCallstack(stack); });
      });
    }
    else
    {
      ui->callstack->setUpdatesEnabled(false);
      ui->callstack->clear();
      ui->callstack->addItem(tr("Callstack resolution not available."));
      ui->callstack->setUpdatesEnabled(true);
    }
  }
  else
  {
    ui->callstack->setUpdatesEnabled(false);
    ui->callstack->clear();
    ui->callstack->addItem(tr("No Callstack available."));
    ui->callstack->setUpdatesEnabled(true);
  }
}

void APIInspector::fillAPIView()
{
  RDTreeWidgetItem *selected = NULL;
  QSignalBlocker selectionSignals(ui->apiEvents);
  ui->apiEvents->beginUpdate();
  ui->apiEvents->clear();

  m_Chunks.clear();

  const ActionDescription *action = m_Ctx.CurSelectedAction();

  if(action != NULL && !action->events.isEmpty())
  {
    if(action->IsFakeMarker())
    {
      RDTreeWidgetItem *root = new RDTreeWidgetItem({lit("---"), QString(action->customName)});
      root->setBold(true);
      ui->apiEvents->addTopLevelItem(root);
      selected = root;
    }
    else
    {
      for(const APIEvent &ev : action->events)
      {
        const bool primary = ev.eventId == action->eventId;
        RDTreeWidgetItem *root = addEvent(ev, primary);
        if(primary)
          selected = root;
      }
    }
  }
  else
  {
    APIEvent ev = m_Ctx.GetEventBrowser()->GetAPIEventForEID(m_Ctx.CurSelectedEvent());

    if(ev.eventId > 0)
      selected = addEvent(ev, true);
  }

  ui->apiEvents->endUpdate();
  selectionSignals.unblock();
  // Intermediate API rows must not each trigger selection, callstack resolution and layout.
  // Select the primary event once, after the model's single reset has completed.
  ui->apiEvents->setSelectedItem(selected);
}

RDTreeWidgetItem *APIInspector::addEvent(const APIEvent &ev, bool primary)
{
  if(ev.chunkIndex == APIEvent::NoChunk)
    return NULL;

  const SDFile &file = m_Ctx.GetStructuredFile();

  RDTreeWidgetItem *root = new RDTreeWidgetItem({QString::number(ev.eventId), QString()});

  SDChunk *chunk = NULL;

  if(ev.chunkIndex < file.chunks.size())
  {
    chunk = file.chunks[ev.chunkIndex];

    m_Chunks.push_back(chunk);

    root->setText(1, SDObject2Variant(chunk, true));

    addStructuredChildren(root, *chunk);
  }
  else
  {
    root->setText(1, tr("Invalid chunk index %1").arg(ev.chunkIndex));
  }

  if(primary)
    root->setBold(true);

  root->setTag(QVariant::fromValue((quintptr)(void *)chunk));

  ui->apiEvents->addTopLevelItem(root);

  return root;
}
