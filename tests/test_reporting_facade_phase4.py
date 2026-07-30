from datetime import datetime
from kiwoom_stock.monitoring.reporter import DailyReporter
from kiwoom_stock.application.reporting import DailyReportResult

class U:
 def __init__(self): self.req=None
 def execute(self, req): self.req=req; return 'ok'

def test_facade_injects_use_case_and_clock():
 u=U(); r=DailyReporter(object(), use_case=u, clock=lambda: datetime(2026,7,19))
 assert r.run_pipeline()=='ok'
 assert u.req.target_date=='2026-07-19'
