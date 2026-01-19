# services/account.py
class AccountService:
    def __init__(self, base):
        self.base = base

    def get_portfolio(self):
        return self.base.request("/api/dostk/acnt", "kt00004", {"qry_tp": "0", "dmst_stex_tp": "KRX"})