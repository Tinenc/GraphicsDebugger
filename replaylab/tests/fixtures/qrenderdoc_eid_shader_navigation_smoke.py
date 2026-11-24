"""Private-process native EID menu/confirmation acceptance on the explicit t3 RDC.

The controller really snapshots Draw/Dispatch stages. Only the navigation reply
is controlled to exercise existing-source, missing/No and missing/Yes UI paths.
No source, project, Shader replacement or real provider request is written.
"""
import json
import os
import sys
import time
import traceback
import renderdoc as rd
from PySide2 import QtCore, QtWidgets


class Probe(object):
    def __init__(self):
        self.app = QtWidgets.QApplication.instance()
        self.ctx = pyrenderdoc
        self.main = self.ctx.GetMainWindow().Widget()
        self.browser = self.ctx.GetEventBrowser().Widget()
        self.bridge = sys.modules['renderdoc_mcp_bridge']
        self.report = {'ok':False, 'cases':[], 'navigationRepliesControlled':True}
        self.finished = False
        self.calls = []
        self.case = 0
        self.deadline = time.monotonic() + 90
        self.mode = 'accepted'
        self.bridge._cancel_project_auto_activation()
        self.old_schedule = self.bridge._schedule_project_auto_activation
        self.bridge._schedule_project_auto_activation = lambda: None
        self.old_navigate = self.bridge.navigate_native_shader_source_json
        def navigate(request):
            value = json.loads(request)
            self.calls.append(value)
            return json.dumps({'accepted':self.mode=='accepted',
                'status':'opened' if self.mode=='accepted' else 'missing',
                'captureSessionId':self.bridge._capture_session_id})
        self.bridge.navigate_native_shader_source_json = navigate

    def later(self, function, delay=30):
        def run():
            if self.finished: return
            try:
                if time.monotonic() > self.deadline: raise RuntimeError('EID navigation smoke timed out')
                function()
            except Exception: self.finish(traceback.format_exc())
        QtCore.QTimer.singleShot(delay, run)

    def start(self):
        root = os.environ['BREAD_DIAG_SOURCE_ROOT']
        self.ctx.LoadCapture('D:/Study/抓帧/终末地/t3.rdc', rd.ReplayOptions(),
            'D:/Study/抓帧/终末地/t3.rdc', False, True)
        self.later(self.next_case, 100)

    def tab_count(self):
        # The production strip is custom painted. Its all-conversations popup
        # exposes the actual items without depending on a save debounce.
        self.bars = [w for w in self.app.allWidgets() if w.objectName()=='aiConversationBar']
        if not self.bars:
            return 0
        self.bar = self.bars[0]
        self.buttons = self.bar.findChildren(QtWidgets.QToolButton)
        button = next((w for w in self.buttons if w.accessibleName()=='Conversations'),None)
        if button is None or not button.isEnabled(): return 0
        button.click()
        self.conversation_popup = self.bar.findChild(QtWidgets.QListWidget)
        count = self.conversation_popup.count()
        self.conversation_popup.close()
        return count

    def next_case(self):
        cases = [(1286,'FS','accepted'),(1475,'CS','accepted'),
                 (1475,'CS','no'),(1475,'CS','yes')]
        if self.case == len(cases):
            self.finish()
            return
        self.event, self.label, self.choice = cases[self.case]
        self.mode = 'accepted' if self.choice=='accepted' else 'missing'
        self.ctx.SetEventID([],self.event,self.event,True)
        self.later(self.open_menu,100)

    def open_menu(self):
        self.tree = self.browser.findChild(QtWidgets.QTreeView,'events')
        assert self.tree is not None
        index = self.tree.currentIndex()
        assert index.isValid()
        self.tree.scrollTo(index)
        point = self.tree.visualRect(index).center()
        assert self.tree.viewport().rect().contains(point)
        self.before_tabs = self.tab_count()
        self.before_calls = len(self.calls)
        self.later(self.inspect_menu,80)
        self.tree.customContextMenuRequested.emit(point)

    def inspect_menu(self):
        self.menu = self.app.activePopupWidget()
        assert isinstance(self.menu,QtWidgets.QMenu)
        self.submenu = None
        self.menu_actions = self.menu.actions()
        for action in self.menu_actions:
            candidate = action.menu()
            if candidate is not None:
                self.submenu = candidate
                break
        assert self.submenu is not None
        self.submenu.popup(self.menu.mapToGlobal(QtCore.QPoint(self.menu.width(),0)))
        self.later(self.inspect_stages)

    def inspect_stages(self):
        labels = [action.text() for action in self.submenu.actions() if action.isEnabled()]
        if not labels:
            self.later(self.inspect_stages)
            return
        assert self.label in labels, labels
        assert ('CS' not in labels if self.event==1286 else labels==['CS']),labels
        assert self.ctx.CurEvent()==self.event
        self.labels = labels
        selected = next(action for action in self.submenu.actions() if action.text()==self.label)
        selected.trigger()
        for menu in (self.submenu,self.menu):
            try: menu.close()
            except RuntimeError: pass
        self.later(self.finish_case,80)

    def finish_case(self):
        if len(self.calls)==self.before_calls:
            self.later(self.finish_case)
            return
        call=self.calls[-1]
        assert call['stage']==('fragment' if self.label=='FS' else 'compute'),call
        assert int(call['resourceId'])>0 and call['entryPoint']
        if self.choice in ('yes','no'):
            boxes=[w for w in self.app.topLevelWidgets() if isinstance(w,QtWidgets.QMessageBox) and w.isVisible()]
            if not boxes:
                self.later(self.finish_case)
                return
            box=boxes[-1]
            assert box.standardButtons() & QtWidgets.QMessageBox.Yes
            box.button(QtWidgets.QMessageBox.Yes if self.choice=='yes' else QtWidgets.QMessageBox.No).click()
        self.later(self.record_case,650)

    def record_case(self):
        after=self.tab_count()
        if self.choice=='no': assert after==self.before_tabs,(self.before_tabs,after)
        if self.choice=='yes': assert after>self.before_tabs,(self.before_tabs,after)
        self.report['cases'].append({'eventId':self.event,'stages':self.labels,
            'choice':self.choice,'requestedStage':self.calls[-1]['stage'],
            'beforeConversations':self.before_tabs,'afterConversations':after})
        self.case+=1
        self.later(self.next_case)

    def finish(self,error=None):
        if self.finished:return
        self.finished=True
        self.report['ok']=error is None
        if error:self.report['error']=error
        self.bridge.navigate_native_shader_source_json=self.old_navigate
        self.bridge._schedule_project_auto_activation=self.old_schedule
        with open(os.environ['BREAD_DIAG_UI_REPORT'],'w',encoding='utf-8') as stream:
            json.dump(self.report,stream,ensure_ascii=False,indent=2)
        self.ctx.GetMainWindow().Widget().close()
        # This fixture's fake provider deliberately has no shutdown RPC.
        QtCore.QTimer.singleShot(200, lambda: os._exit(0))


def start_probe():
    try:
        probe = Probe()
        probe.app._bread_navigation_smoke = probe
        probe.later(probe.start, 50)
    except Exception:
        with open(os.environ['BREAD_DIAG_UI_REPORT'], 'w', encoding='utf-8') as stream:
            json.dump({'ok':False, 'error':traceback.format_exc()}, stream)
        QtCore.QTimer.singleShot(100, lambda: os._exit(1))


pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start_probe)
