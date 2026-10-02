'''
Primary public module for eloverblik.dk API wrapper.
'''
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from os import access
import re
import requests
import logging
from .models import RawResponse
from .models import TimeSeries
from .models import Charges
from .models import MeterReading
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

_LOGGER = logging.getLogger(__name__)

# 400 is not retried: the API uses it for invalid parameters, which never succeed on retry.
# For 429 and 503 the API asks clients to wait a minute before retrying.
retry_strategy = Retry(
    total=3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    backoff_factor=60
)
adapter = HTTPAdapter(max_retries=retry_strategy)
http = requests.Session()
http.mount("https://", adapter)

# Quality code for points where the grid operator submitted a missing indicator and no quantity.
QUALITY_NOT_AVAILABLE = 'A02'

TARIFF_PERIOD_DAY = ('P1D', 'DAY')
TARIFF_PERIOD_HOUR = ('PT1H', 'HOUR')


class Eloverblik:
    '''
    Primary exported interface for eloverblik.dk API wrapper.
    '''

    # Access tokens keyed by refresh token, so instances for different users don't share tokens.
    _access_token_cache = {}

    def __init__(self, refresh_token):
        self._refresh_token = refresh_token
        self._base_url = 'https://api.eloverblik.dk/CustomerApi/'

    def get_time_series(self,
                        meetering_point,
                        from_date=None,  # Will be set to yesterday
                        to_date=None,  # Will be set to today
                        aggregation='Hour'):
        '''
        Call time series API on eloverblik.dk. Defaults to yester days data.
        '''
        if from_date is None:
            from_date = datetime.now()-timedelta(days=1)
        if to_date is None:
            to_date = datetime.now()

        # The API rejects equal dates (error 30002), e.g. 1 January for the current year.
        if to_date.date() <= from_date.date():
            to_date = from_date + timedelta(days=1)

        access_token = self._get_access_token()

        date_format = '%Y-%m-%d'
        parsed_from_date = from_date.strftime(date_format)
        parsed_to_date = to_date.strftime(date_format)
        body = "{\"meteringPoints\": {\"meteringPoint\": [\"" + \
            meetering_point + "\"]}}"

        headers = self._create_headers(access_token)

        url = self._base_url + \
            f'/api/MeterData/GetTimeSeries/{parsed_from_date}/{parsed_to_date}/{aggregation}'
        response = http.post(url,
                                 data=body,
                                 headers=headers,
                                 timeout=5
                                 )

        _LOGGER.debug(
            f"Response from API. Status: {response.status_code}, Body: {response.text}")

        raw_response = RawResponse()
        raw_response.status = response.status_code
        raw_response.body = response.text

        return raw_response

    def get_tariffs(self,
                    metering_point):
        '''
        Call charges API on eloverblik.dk and extract tariffs. Note that this does not include subscriptions or fees.
        '''

        access_token = self._get_access_token()
        headers = self._create_headers(access_token)
        body = '{"meteringPoints": {"meteringPoint": ["' + metering_point + '"]}}'
        url = self._base_url + '/api/meteringpoints/meteringpoint/getcharges'

        response = http.post(url,
                                 data=body,
                                 headers=headers,
                                 timeout=5
                                 )

        _LOGGER.debug(
            f"Response from API. Status: {response.status_code}, Body: {response.text}")

        if response.status_code == 200:
            return self._parse_tariffs_from_charges_result(json.loads(response.text))
        else:
            return Charges(response.status_code, None, response.text)

    def get_meter_reading_latest(self, metering_point):
        '''
        Get latest hourly consumption from the time series API on eloverblik.dk. Will look for 90 days.
        '''
        raw_data = self.get_time_series(metering_point,
                                        from_date=datetime.now()-timedelta(days=90),
                                        to_date=datetime.now(),
                                        aggregation='Hour')

        if raw_data.status == 200:
            return self._parse_latest_hour(json.loads(raw_data.body))
        else:
            return MeterReading(raw_data.status, None, None, detailed_status=raw_data.body)

    def _parse_latest_hour(self, result) -> MeterReading:
        '''
        Parse time series result from API call and return the most recent hourly point.
        '''
        results, error = self._successful_results(result)

        if error is not None:
            return MeterReading(404, None, None, detailed_status=error)

        latest_end = None
        latest_quantity = None
        unit = None

        for time_series in self._time_series(results):
            unit = time_series.get('measurement_Unit.name')

            for period in time_series.get('Period') or []:
                period_start = self._parse_api_datetime(period['timeInterval']['start'])

                for point in period.get('Point') or []:
                    quantity = self._point_quantity(point)
                    if quantity is None:
                        continue

                    # Position 1 is the hour starting at period start, so its end is start + 1 hour.
                    point_end = period_start + timedelta(hours=int(point['position']))

                    if latest_end is None or point_end > latest_end:
                        latest_end = point_end
                        latest_quantity = quantity

        if latest_end is None:
            return MeterReading(404, None, None, detailed_status="Result does not contain any hourly data.")

        return MeterReading(200, latest_quantity, latest_end.isoformat(), unit)

    def _get_access_token(self):
        cache_datetime, short_token = Eloverblik._access_token_cache.get(self._refresh_token, (None, None))

        if cache_datetime is not None and datetime.today() - cache_datetime < timedelta(hours = 12):
            _LOGGER.debug("Found valid token in cache.")
            return short_token

        url = self._base_url + 'api/Token'
        headers = {'Authorization': 'Bearer ' + self._refresh_token}

        token_response = http.get(url, headers=headers, timeout=5)
        token_response.raise_for_status()

        token_json = token_response.json()
        short_token = token_json['result']

        Eloverblik._access_token_cache[self._refresh_token] = (datetime.today(), short_token)

        _LOGGER.debug(f"Got short lived token: {short_token}")
        return short_token

    def _create_headers(self, access_token):
        return {'Authorization': 'Bearer ' + access_token,
                'Content-Type': 'application/json',
                'Accept': 'application/json'}

    def get_yesterday_parsed(self, metering_point):
        '''
        Get data for yesterday and parse it.
        '''
        raw_data = self.get_time_series(metering_point)

        if raw_data.status == 200:
            json_response = json.loads(raw_data.body)

            result_dict = self._parse_result(json_response)
            (key, value) = result_dict.popitem()
            result = value
        else:
            result = TimeSeries(raw_data.status, None, None, raw_data.body)

        return result

    def get_latest(self, metering_point):
        '''
        Get latest data. Will look for one week.
        '''
        raw_data = self.get_time_series(metering_point,
                                        from_date=datetime.now()-timedelta(days=8),
                                        to_date=datetime.now())

        if raw_data.status == 200:
            json_response = json.loads(raw_data.body)

            r = self._parse_result(json_response)

            keys = list(r.keys())

            keys.sort()
            keys.reverse()

            result = r[keys[0]]
        else:
            result = TimeSeries(raw_data.status, None, None, raw_data.body)

        return result

    def get_per_month(self, metering_point, year=None):
        '''
        Get total consumption for each month in the given year, as well as the total for the year.
        '''
        if year is None:
            year = datetime.today().year

        if not re.match(r'\d{4}', str(year)):
            raise ValueError("Year must be a four digit number.")

        raw_data = self.get_time_series(metering_point,
                                        from_date=datetime(year, 1, 1),
                                        to_date=datetime(year, 12, 31) if year < datetime.today().year else datetime.today(),
                                        aggregation='Month')

        if raw_data.status == 200:
            json_response = json.loads(raw_data.body)

            r = self._parse_result(json_response)
            if 'none' in r:
                return r['none']

            keys = list(r.keys())
            keys.sort()

            result = TimeSeries(raw_data.status, keys[-1], [r[k].get_total_metering_data() for k in keys])
        else:
            result = TimeSeries(raw_data.status, None, None, raw_data.body)

        return result


    def _parse_result(self, result):
        '''
        Parse result from API call.
        Returns TimeSeries keyed by period end, or a single 'none' key with a 404 TimeSeries if there is no data.
        Points that are missing or have no quantity are None, so each value stays at its position.
        '''
        results, error = self._successful_results(result)

        if error is not None:
            return {'none': TimeSeries(404, None, None, error)}

        parsed_result = {}

        for time_series in self._time_series(results):
            for period in time_series.get('Period') or []:
                start = self._parse_api_datetime(period['timeInterval']['start'])
                end = self._parse_api_datetime(period['timeInterval']['end'])
                points = period.get('Point') or []

                length = max([int(p['position']) for p in points], default=0)
                if period.get('resolution') == 'PT1H':
                    # Also covers trailing missing hours, and 23/25 hour days.
                    length = max(length, int((end - start).total_seconds() // 3600))

                metering_data = [None] * length
                for point in points:
                    metering_data[int(point['position']) - 1] = self._point_quantity(point)

                parsed_result[end] = TimeSeries(200, end, metering_data)

        if len(parsed_result) == 0:
            parsed_result['none'] = TimeSeries(404,
                                               None,
                                               None,
                                               f"Data most likely not available yet: {result}")

        return parsed_result

    def _successful_results(self, result):
        '''
        Return the successful per metering point results and an error text if there are none.
        A HTTP 200 response can still contain results with success=false and an errorCode.
        '''
        results = result.get('result') or []

        if len(results) == 0:
            return [], "Result does not contain any data."

        successful = [r for r in results if r.get('success', True)]

        if len(successful) == 0:
            errors = ', '.join(f"Error {r.get('errorCode')}: {r.get('errorText')}" for r in results)
            return [], errors

        return successful, None

    def _time_series(self, results):
        '''
        All TimeSeries in the given results. The same metering point can be returned once per access period.
        '''
        for r in results:
            market_document = r.get('MyEnergyData_MarketDocument') or {}
            yield from market_document.get('TimeSeries') or []

    def _point_quantity(self, point):
        '''
        Quantity of a time series point, or None if the grid operator reported it as not available.
        '''
        quantity = point.get('out_Quantity.quantity')

        if quantity is None or point.get('out_Quantity.quality') == QUALITY_NOT_AVAILABLE:
            return None

        return float(quantity)

    def _parse_api_datetime(self, value):
        '''
        Parse a timestamp from the API. Timestamps without a timezone are assumed to be UTC.
        '''
        parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed

    def _tariff_is_valid_now(self, tariff, now):
        '''
        The charges API also returns charges that start in the future. Those should not be part of todays price.
        '''
        try:
            valid_from = self._parse_api_datetime(tariff['validFromDate']) if tariff.get('validFromDate') else None
            valid_to = self._parse_api_datetime(tariff['validToDate']) if tariff.get('validToDate') else None
        except ValueError:
            _LOGGER.warning(f"Unable to parse validity dates for tariff '{tariff.get('name')}'. Including it.")
            return True

        if valid_from is not None and valid_from > now:
            return False

        if valid_to is not None and valid_to <= now:
            return False

        return True

    def _parse_tariffs_from_charges_result(self, result):
        '''
        Parse charges result from API call
        '''
        results, error = self._successful_results(result)

        if error is None and not (results[0].get('result') or {}).get('tariffs'):
            error = "Result does not contain any tariffs."

        if error is not None:
            return Charges(404, None, error)

        charges = {}
        now = datetime.now(timezone.utc)

        for tariff in results[0]['result']['tariffs']:
            if not self._tariff_is_valid_now(tariff, now):
                _LOGGER.debug(f"Skipping tariff '{tariff['name']}' that is not valid now ({tariff.get('validFromDate')} - {tariff.get('validToDate')}).")
                continue

            name = tariff['name'].lower().replace(' ', '_')
            if name in charges:
                # Different tariffs can have the same name. Keep both, so both are part of the sum.
                name = f"{name}_{tariff.get('priceId') or len(charges)}"

            if tariff['periodType'] in TARIFF_PERIOD_DAY:
                charges[name] = tariff['prices'][0]['price']
            elif tariff['periodType'] in TARIFF_PERIOD_HOUR:
                sorted_prices = [p['price'] for p in sorted(tariff['prices'], key=lambda d: int(d['position']))]
                charges[name] = sorted_prices
            else:
                _LOGGER.warning(f"Unsupported periodType '{tariff['periodType']}' for tariff '{tariff['name']}'. It is not included in the tariff sum.")

        return Charges(200, charges)
