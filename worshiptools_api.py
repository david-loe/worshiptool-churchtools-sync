import logging
import requests
import urllib.parse


class Worshiptools_API:
    def __init__(self, email, password, account_id):
        self.email = email
        self.password = password
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Connection": "keep-alive",
            }
        )
        self.bearer_token = None
        self._login()

    def _login(self):
        initial_url = "https://planning.worshiptools.com/app"
        response = self.session.get(initial_url, allow_redirects=True)
        if response.status_code not in [200, 302]:
            raise Exception(
                f"Fehler beim Abrufen von authRequest oder weAuthState: {response.status_code}, {response.text}"
            )
        login_url = "https://auth.worshiptools.com/login"
        self.session.headers.update({"Content-Type": "application/x-www-form-urlencoded"})
        data = {
            "email": self.email,
            "password": self.password,
        }
        response = self.session.post(login_url, data=data, allow_redirects=True)
        if response.status_code not in [200, 302]:
            raise Exception(f"Fehler beim Login: {response.status_code}, {response.text}")
        logging.info(f"Worshiptools Login Successful as {self.email}")
        self.bearer_token = self.session.cookies.get("weAuthToken")

    def get(self, endpoint: str, params: dict = {}):
        params_str = ""
        if params:
            params_str = "?" + urllib.parse.urlencode(params)
        api_url = f"https://api.worship.tools/v1/account/{self.account_id}/{endpoint}{params_str}"
        logging.info(f"GET {api_url}")
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
                "Origin": "https://planning.worshiptools.com",
            }
        )
        response = self.session.get(api_url)
        if response.status_code == 200:
            return response.json().get("response")
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def get_all(self, endpoint: str, params: dict = {}):
        current_num = 0
        total_num = 1
        data = []
        while current_num < total_num:
            params.update({"start": current_num})
            res = self.get(endpoint, params)
            if not res:
                raise Exception("Fehler bei Anfrage an Worshiptools API")
            total_num = res["numFound"]
            data = data + res["docs"]
            current_num = len(data)
        return {"docs": data}
