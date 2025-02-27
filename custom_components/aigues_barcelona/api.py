import base64
import datetime
import json
import logging

import requests

from .const import API_COOKIE_TOKEN
from .const import API_HOST
from .version import VERSION

TIMEOUT = 60

_LOGGER: logging.Logger = logging.getLogger(__name__)


class AiguesApiClient:
    def __init__(
        self,
        username,
        password,
        contract=None,
        session: requests.Session = None,
        company_identification=None,
    ):
        if session is None:
            session = requests.Session()
        self.cli = session
        self.api_host = f"https://{API_HOST}"
        # https://www.aiguesdebarcelona.cat/o/ofex-theme/js/chunk-vendors.e5935b72.js
        # https://www.aiguesdebarcelona.cat/o/ofex-theme/js/app.0499d168.js
        self.headers = {
            "Ocp-Apim-Subscription-Key": "3cca6060fee14bffa3450b19941bd954",
            "Ocp-Apim-Trace": "false",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": f"hass-aigues-barcelona/{VERSION} (Home Assistant)",
        }
        self._username = username
        self._password = password
        self._contract = contract
        self._company_identification = company_identification
        self.last_response = None

    def _generate_url(self, path, query) -> str:
        query_proc = ""
        if query:
            query_proc = "?" + "&".join([f"{k}={v}" for k, v in query.items()])
        return f"{self.api_host}/{path.lstrip('/')}{query_proc}"

    def _return_token_field(self, key):
        token = self.cli.cookies.get_dict().get(API_COOKIE_TOKEN)
        if not token:
            return False

        data = token.split(".")[1]
        # add padding to avoid failures
        data = base64.urlsafe_b64decode(data + "==")

        return json.loads(data).get(key)

    def _query(self, path, query=None, json=None, headers=None, method="GET"):
        if headers is None:
            headers = dict()
        headers = {**self.headers, **headers}

        try:
            resp = self.cli.request(
                method=method,
                url=self._generate_url(path, query),
                json=json,
                headers=headers,
                timeout=TIMEOUT,
            )
            _LOGGER.debug(f"Query done with code {resp.status_code}")

            # Store raw response first
            self.last_response = resp.text

            # Try to parse JSON response if possible
            try:
                if resp.text and len(resp.text) > 0:
                    msg = resp.json()
                    if isinstance(msg, list) and len(msg) == 1:
                        msg = msg[0]
                    self.last_response = msg
            except json.JSONDecodeError:
                msg = resp.text
                _LOGGER.debug(f"Response is not JSON: {msg}")

            if resp.status_code == 503:
                raise Exception("Service temporarily unavailable")
            if resp.status_code == 500:
                raise Exception(f"Server error: {msg}")
            if resp.status_code == 404:
                raise Exception(f"Not found: {msg}")
            if resp.status_code == 401:
                raise Exception(f"Denied: {msg}")
            if resp.status_code == 400:
                raise Exception(f"Bad response: {msg}")
            if resp.status_code == 429:
                raise Exception(f"Rate-Limited: {msg}")

            return resp

        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"Request failed: {str(e)}")
            raise Exception(f"Request failed: {str(e)}")

    def login(self, user=None, password=None, recaptcha=None):
        if user is None:
            user = self._username
        if password is None:
            password = self._password
        # recaptcha seems to not be validated?
        if recaptcha is None:
            recaptcha = ""

        path = "/ofex-login-api/auth/getToken"
        query = {"lang": "ca", "recaptchaClientResponse": recaptcha}
        body = {
            "scope": "ofex",
            "companyIdentification": self._company_identification or "",
            "userIdentification": user,
            "password": password,
        }
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": "6a98b8b8c7b243cda682a43f09e6588b;product=portlet-login-ofex",
        }

        r = self._query(path, query, body, headers, method="POST")

        error = r.json().get("errorMessage", None)
        if error:
            return False

        access_token = r.json().get("access_token", None)
        if not access_token:
            return False

        return True

        # set as cookie: ofexTokenJwt
        # https://www.aiguesdebarcelona.cat/ca/area-clientes

    def set_token(self, token: str):
        host = ".".join(self.api_host.split(".")[1:])
        cookie_data = {
            "name": API_COOKIE_TOKEN,
            "value": token,
            "domain": f".{host}",
            "path": "/",
            "secure": True,
            "rest": {"HttpOnly": True, "SameSite": "None"},
        }
        cookie = requests.cookies.create_cookie(**cookie_data)
        return self.cli.cookies.set_cookie(cookie)

    def is_token_expired(self) -> bool:
        """Check if Token in cookie has expired or not."""
        expires = self._return_token_field("exp")
        if not expires:
            return True

        expires = datetime.datetime.fromtimestamp(expires)
        NOW = datetime.datetime.now()

        return NOW >= expires

    def profile(self, user=None):
        if user is None:
            user = self._return_token_field("name")

        path = "/ofex-login-api/auth/getProfile"
        query = {"lang": "ca", "userId": user, "clientId": user}
        headers = {
            "Ocp-Apim-Subscription-Key": "6a98b8b8c7b243cda682a43f09e6588b;product=portlet-login-ofex"
        }

        r = self._query(path, query, headers=headers, method="POST")

        assert r.json().get("user_data"), "User data missing"
        return r.json()

    def contracts(self, user=None, status=None):
        if user is None:
            user = self._return_token_field("name")
        if status is None:
            status = ["ASSIGNED", "PENDING"]

        path = "/ofex-contracts-api/contracts"
        query = {
            "userId": user,
            "clientId": self._company_identification or user,
            "lang": "ca",
        }

        # Add each status as a separate query parameter
        for stat in status:
            if "assignationStatus" not in query:
                query["assignationStatus"] = stat.upper()
            else:
                # Append additional status values
                query["assignationStatus"] = (
                    f"{query['assignationStatus']}&assignationStatus={stat.upper()}"
                )

        r = self._query(path, query)

        data = r.json().get("data")
        return data

    @property
    def contract_id(self):
        return [x["contractDetail"]["contractNumber"] for x in self.contracts()]

    @property
    def first_contract(self):
        contract_ids = self.contract_id
        assert (
            len(contract_ids) == 1
        ), "Provide a Contract ID to retrieve specific invoices"
        return contract_ids[0]

    def invoices(self, contract=None, user=None, last_months=36, mode="ALL"):
        if user is None:
            user = self._return_token_field("name")
        if contract is None:
            contract = self.first_contract

        path = "/ofex-invoices-api/invoices"
        query = {
            "contractNumber": contract,
            "userId": user,
            "clientId": user,
            "lang": "ca",
            "lastMonths": last_months,
            "mode": mode,
        }

        r = self._query(path, query)

        data = r.json().get("data")
        return data

    def invoices_debt(self, contract=None, user=None):
        return self.invoices(contract, user, last_months=0, mode="DEBT")

    def consumptions(
        self, date_from, date_to, contract=None, user=None, frequency="HOURLY"
    ):
        if user is None:
            user = self._username
        if contract is None:
            contract = self._contract

        path = "/ofex-water-consumptions-api/meter/consumptions"
        query = {
            "consumptionFrequency": frequency,
            "contractNumber": contract,
            "clientId": self._company_identification or user,
            "userId": user,
            "lang": "ca",
            "fromDate": date_from.strftime("%d-%m-%Y"),
            "toDate": date_to.strftime("%d-%m-%Y"),
            "showNegativeValues": "false",
        }

        r = self._query(path, query)

        data = r.json().get("data")
        return data

    def consumptions_week(self, date_from: datetime.date, contract=None, user=None):
        if date_from is None:
            date_from = datetime.datetime.now()
        # get first day of week
        monday = date_from - datetime.timedelta(days=date_from.weekday())
        sunday = monday + datetime.timedelta(days=6)
        return self.consumptions(monday, sunday, contract, user, frequency="DAILY")

    def consumptions_month(self, date_from: datetime.date, contract=None, user=None):
        first = date_from.replace(day=1)
        next_month = date_from.replace(day=28) + datetime.timedelta(days=4)
        last = next_month - datetime.timedelta(days=next_month.day)
        return self.consumptions(first, last, contract, user, frequency="DAILY")

    def parse_consumptions(self, info, key="accumulatedConsumption"):
        return [x[key] for x in info]
