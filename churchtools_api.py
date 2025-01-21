from datetime import datetime, date
from decimal import Decimal
import json
import logging
from typing import Dict, Optional
import requests
import urllib.parse


class Churchtools_API:
    def __init__(
        self,
        base_url: str,
        ct_token: Optional[str] = None,
        ct_user: Optional[str] = None,
        ct_password: Optional[str] = None,
    ):
        """Setup of a ChurchToolsApi object for the specified ct_domain using a token login.

        Arguments:
            base_url: including https:// ending on e.g. .de
            ct_token: direct access using a user token
            ct_user: indirect login using user and password combination
            ct_password: indirect login using user and password combination

        """
        self.session = None
        self.base_url = base_url
        self.ajax_song_last_update = None
        self.ajax_song_cache = []

        if ct_token is not None:
            self.login_ct_rest_api(ct_token=ct_token)
        elif ct_user is not None and ct_password is not None:
            self.login_ct_rest_api(ct_user=ct_user, ct_password=ct_password)

        logging.debug("ChurchToolsApi init finished")

    def login_ct_rest_api(self, **kwargs):
        """Authorization: Login<token>
        If you want to authorize a request, you need to provide a Login Token as
        Authorization header in the format {Authorization: Login<token>}
        Login Tokens are generated in "Berechtigungen" of User Settings
        using REST API login as opposed to AJAX login will also save a cookie.

        :param kwargs: optional keyword arguments as listed
        :keyword ct_token: str : token to be used for login into CT
        :keyword ct_user: str: the username to be used in case of unknown login token
        :keyword ct_password: str: the password to be used in case of unknown login token
        :return: personId if login successful otherwise False
        :rtype: int | bool
        """
        self.session = requests.Session()

        if "ct_token" in kwargs:
            logging.info("Trying Login with token")
            url = self.base_url + "/api/whoami"
            headers = {"Authorization": "Login " + kwargs["ct_token"]}
            response = self.session.get(url=url, headers=headers)

            if response.status_code == 200:
                response_content = json.loads(response.content)
                logging.info(
                    "Token Login Successful as %s",
                    response_content["data"]["email"],
                )
                self.session.headers["CSRF-Token"] = self.get_ct_csrf_token()
                return json.loads(response.content)["data"]["id"]
            logging.warning(
                "Token Login failed with %s",
                response.content.decode(),
            )
            return False

        if "ct_user" in kwargs and "ct_password" in kwargs:
            logging.info("Trying Login with Username/Password")
            url = self.base_url + "/api/login"
            data = {"username": kwargs["ct_user"], "password": kwargs["ct_password"]}
            response = self.session.post(url=url, data=data)

            if response.status_code == 200:
                logging.info("User/Password Login Successful")
                self.session.headers["CSRF-Token"] = self.get_ct_csrf_token()
                return json.loads(response.content)["data"]["id"]
            logging.warning(
                "User/Password Login failed with %s",
                response.content.decode(),
            )
            return False
        return None

    def get_ct_csrf_token(self):
        """Requests CSRF Token https://hilfe.church.tools/wiki/0/API-CSRF
        Storing and transmitting CSRF token in headers is required for all legacy AJAX API calls unless disabled by admin
        Therefore it is executed with each new login.

        :return: token
        :rtype: str
        """
        url = self.base_url + "/api/csrftoken"
        response = self.session.get(url=url)
        if response.status_code == 200:
            csrf_token = json.loads(response.content)["data"]
            logging.debug("CSRF Token erfolgreich abgerufen %s", csrf_token)
            return csrf_token
        logging.warning(
            "CSRF Token not updated because of Response %s",
            response.content.decode(),
        )
        return None

    def save_item_ajax(self, data: Dict[str, str | int]):
        """
        data: {"id":"83197","agenda_id":"3299","arrangement_id":"233","func":"saveItem", "bezeichnung": "", "header_yn": "0", "responsible":"[Lobpreisleitung]", "sortkey":12, "duration":300}
        """
        api_url = f"{self.base_url}/index.php?q=churchservice/ajax"
        data["func"] = "saveItem"
        json_data = json.dumps(data, cls=CustomEncoder)
        logging.info(f"POST {api_url}\n{json_data}")

        response = self.session.post(api_url, data=json_data, headers={"Content-Type": "application/json"})

        if response.status_code == 200:
            return response.json()
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def add_item_event_relation(self, item_id: int, event_id: int):
        api_url = f"{self.base_url}/index.php?q=churchservice/ajax"
        data = {"func": "addItemEventRelation", "item_id": item_id, "event_id": event_id}
        logging.info(f"POST {api_url}\n{data}")

        response = self.session.post(api_url, data=data)

        if response.status_code == 200:
            return json.loads(response.content)
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def load_agenda_ajax(self, agenda_id: int):
        api_url = f"{self.base_url}/index.php?q=churchservice/ajax"
        data = {"func": "loadAgendas", "ids[0]": agenda_id}
        logging.info(f"POST {api_url}\n{data}")

        response = self.session.post(api_url, data=data)

        if response.status_code == 200:
            return json.loads(response.content)
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def save_agenda_ajax(self, data):
        api_url = f"{self.base_url}/index.php?q=churchservice/ajax"
        data["func"] = "saveAgenda"
        json_data = json.dumps(data, cls=CustomEncoder)
        logging.info(f"POST {api_url}\n{json_data}")

        response = self.session.post(api_url, data=json_data, headers={"Content-Type": "application/json"})

        if response.status_code == 200:
            return response.json()
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def load_agenda_items_ajax(self, agenda_id: int):
        api_url = f"{self.base_url}/index.php?q=churchservice/ajax"
        data = {"func": "loadAgendaItems", "agenda_id": agenda_id}
        logging.info(f"POST {api_url}\n{data}")

        response = self.session.post(api_url, data=data)

        if response.status_code == 200:
            return json.loads(response.content)
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def create_song(
        self,
        title: str,
        songcategory_id: int,
        author="",
        copyright="",
        ccli="",
        tonality="",
        bpm=None,
        beat=None,
    ):
        """Method to create a new song using legacy AJAX API
        Does not check for existing duplicates !
        function endpoint see https://api.church.tools/function-churchservice_addNewSong.html
        name for params reverse engineered based on web developer tools in Firefox and live churchTools instance.

        :param title: Title of the Song
        :param songcategory_id: int id of site specific songcategories (created in CT Metadata) - required
        :param author: name of author or authors, ideally comma separated if multiple - optional
        :param copyright: name of organization responsible for rights distribution - optional
        :param ccli: CCLI ID see songselect.ccli.com/ - using "-" if empty on purpose - optional
        :param tonality: empty or specific string used for tonaly - see ChurchTools for details e.g. Ab,A,C,C# ... - optional
        :param bpm: Beats per Minute - optional
        :param beat: Beat - optional

        :return: int song_id: ChurchTools song_id of the Song created or None if not successful
        :rtype: int | None
        """
        url = self.base_url + "/?q=churchservice/ajax&func=addNewSong"

        data = {
            "bezeichnung": title,
            "songcategory_id": songcategory_id,
            "author": author,
            "copyright": copyright,
            "ccli": ccli,
            "tonality": tonality,
            "bpm": bpm,
            "beat": beat,
        }
        logging.info(f"POST {url}\n{data}")
        response = self.session.post(url=url, data=data)

        if response.status_code == 200:
            response_content = json.loads(response.content)
            new_id = int(response_content["data"])
            logging.debug("Song created successful with ID=%s", new_id)
            return new_id

        logging.info("Creating song failed with %s", response.status_code)
        return None

    def get(self, endpoint: str, params={}):
        params_str = ""
        if params:
            params_str = "?" + urllib.parse.urlencode(params)
        api_url = f"{self.base_url}/api/{endpoint}{params_str}"
        logging.info(f"GET {api_url}")
        response = self.session.get(api_url)
        if response.status_code == 200:
            return response.json()
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None

    def get_all(self, endpoint: str, params: dict = {}):
        current_page = 0
        last_page = 1
        data = []
        while current_page < last_page:
            current_page = current_page + 1
            params.update({"page": current_page})
            res = self.get(endpoint, params)
            last_page = res["meta"]["pagination"]["lastPage"]
            data = data + res["data"]
        return {"data": data}

    def get_event_masterdata(self, **kwargs) -> list | list[list] | dict | list[dict]:
        """Function to get the Masterdata of the event module.
        This information is required to map some IDs to specific items.

        Params
            kwargs: optional keywords as listed below

        Keywords:
            type: str with name of the masterdata type (not datatype) common types are 'absenceReasons', 'songCategories', 'services', 'serviceGroups'
            returnAsDict: if the list with one type should be returned as dict by ID

        Returns:
            list of masterdata items, if multiple types list of lists (by type).
        """
        url = self.base_url + "/api/event/masterdata"

        headers = {"accept": "application/json"}
        response = self.session.get(url=url, headers=headers)

        if response.status_code == 200:
            response_content = json.loads(response.content)
            response_data = response_content["data"].copy()

            if "type" in kwargs:
                response_data = response_data[kwargs["type"]]
                if kwargs.get("returnAsDict"):
                    response_data2 = response_data.copy()
                    response_data = {item["id"]: item for item in response_data2}
            logging.debug("Event Masterdata load successful len=%s", response_data)

            return response_data
        logging.info(
            "Event Masterdata requested failed: %s",
            response.status_code,
        )
        return None

    def post(self, endpoint: str, data):
        api_url = f"{self.base_url}/api/{endpoint}"
        json_data = json.dumps(data, cls=CustomEncoder)
        logging.info(f"POST {api_url}\n{json_data}")
        response = self.session.post(
            api_url,
            data=json_data,
            headers={"Content-Type": "application/json", "Authorization": f"Login {self.login_token}"},
        )
        if response.status_code == 200:
            return response.json()
        logging.error(f"Fehler bei der API-Anfrage: {response.status_code}, {response.text}")
        return None


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return float(obj)  # or str(obj) if you prefer
        return super(CustomEncoder, self).default(obj)
