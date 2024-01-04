import threading
import time
import requests
import concurrent.futures
import os
import json
from queue import Queue
from ratelimit import limits, sleep_and_retry
from datetime import datetime, timedelta
from tqdm import tqdm

from handlers import utils
from handlers.logger_handler import Logger
from handlers.config_handler import ConfigHandler
from handlers.mongodb_handler import MongodbHandler

def singleton(cls):
    instances = {}

    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]

    return get_instance

@singleton
class NvdHandler:

    def __init__(self, config_file='configuration.ini'):
        self.banner = f"{chr(int('EAD3', 16))} {chr(int('f0626', 16))} CVE from NVD"

        config_handler = ConfigHandler(config_file)

        nvd_config = config_handler.get_nvd_config()
        self.baseurl = nvd_config.get('url', 'https://services.nvd.nist.gov/rest/json/cves/2.0')
        self.api_key = nvd_config.get('apikey', '')
        self.public_rate_limit = int(nvd_config.get('public_rate_limit', 5))
        self.api_rate_limit = int(nvd_config.get('apikey_rate_limit', 50))
        self.rolling_window = int(nvd_config.get('rolling_window', 30))
        self.retry_limit = int(nvd_config.get('retry_limit', 3))
        self.retry_delay = int(nvd_config.get('retry_delay', 10))
        self.results_per_page = int(nvd_config.get('results_per_page', 2000))
        self.max_threads = int(nvd_config.get('max_threads', 10))

        self.save_data = config_handler.get_boolean('nvd', 'save_data', False)

        if self.save_data:
            output_directory = os.path.dirname("data")
            if not os.path.exists(output_directory):
                os.makedirs(output_directory)

        mongodb_config = config_handler.get_mongodb_config()        
        self.mongodb_handler = MongodbHandler(
            mongodb_config['host'],
            mongodb_config['port'],
            mongodb_config['db'],
            mongodb_config['username'],
            mongodb_config['password'],
            mongodb_config['authdb'],
            mongodb_config['prefix'])
    

    def make_request(self, step="update", start_index=0, custom_params=None):
        
        @sleep_and_retry
        @limits(calls=self.api_rate_limit, period=self.rolling_window)
        def _make_request_limited():
            params = {
                'resultsPerPage': self.results_per_page,
                'startIndex': start_index
            }
            if custom_params:
                params.update(custom_params)

            headers = {'apiKey': self.api_key} if self.api_key else {}

            # Construct the full URL for error reporting
            full_url = requests.Request('GET', self.baseurl, headers=headers, params=params).prepare().url
        
            response = requests.get(self.baseurl, headers=headers, params=params)
            if response.status_code != 200:
                error_msg = f'Error {response.status_code} when accessing URL: {full_url}'
                raise Exception(error_msg)

            try:
                return response.json()
            except ValueError:
                raise ValueError(f"Invalid JSON response received from URL: {full_url}")

        data = _make_request_limited()
        
        vulnerabilities = [
            vul.get('cve', {})
            for vul in data.get('vulnerabilities', [])
        ]

        if vulnerabilities:
            if step.lower() == "init":
                self.mongodb_handler.insert_many("cve", vulnerabilities, silent=True)
            else:
                self.mongodb_handler.bulk_write("cve", vulnerabilities, silent=True)

            self.mongodb_handler.update_status("nvd", silent=True)
        
        
        return data


    def download_all_data(self):
        print("\n"+self.banner)
        initial_response = self.make_request()
        initial_vulnerabilities = initial_response.get('vulnerabilities', [])

        total_results = initial_response.get('totalResults', 0)
        num_pages = (total_results + self.results_per_page - 1) // self.results_per_page

        all_vulnerabilities = []  # List to store all vulnerabilities

        # with tqdm(total=total_results) as pbar:
        with tqdm(total=total_results, initial=len(initial_vulnerabilities)) as pbar:
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:          
                # Start from the second page, since the first page was already fetched  
                futures = [executor.submit(self.make_request, step="init", start_index=(start_index * self.results_per_page))
                           for start_index in range(1, num_pages)]

                for future in concurrent.futures.as_completed(futures):
                    data = future.result()
                    vulnerabilities = data.get('vulnerabilities', [])
                    all_vulnerabilities.extend(vulnerabilities)  # Append vulnerabilities to the list
                    pbar.update(len(vulnerabilities))

        self.mongodb_handler.ensure_index_on_id("cve","id")

        if self.save_data:
            utils.write2json("data/nvd_all.json", all_vulnerabilities)


    def get_updates(self, last_hours=None, follow=True):
        print("\n"+self.banner)
        last_update_time = self.mongodb_handler.get_last_update_time("nvd")
        now_utc = datetime.utcnow()

        if last_hours:
            lastModStartDate = now_utc - timedelta(hours=last_hours)
        elif last_update_time:
            lastModStartDate = last_update_time
        else:
            lastModStartDate = now_utc - timedelta(hours=24)

        lastModStartDate_str = lastModStartDate.strftime('%Y-%m-%dT%H:%M:%SZ')
        lastModEndDate_str = now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Calculate the duration of the window in a human-readable format
        duration = now_utc - lastModStartDate
        days, seconds = duration.days, duration.seconds
        hours = days * 24 + seconds // 3600
        minutes = (seconds % 3600) // 60
        duration_str = f"{days} days, {hours % 24} hours, {minutes} minutes" if days else f"{hours % 24} hours, {minutes} minutes"

        # Log message with time window and its human-readable duration
        Logger.log(f"Downloading data for the window: Start - {lastModStartDate_str}, End - {lastModEndDate_str} (Duration: {duration_str})", "INFO")

        custom_params = {
            "lastModStartDate": lastModStartDate_str,
            "lastModEndDate": lastModEndDate_str
        }

        updates = self.make_request(custom_params=custom_params)

        if self.save_data:
            utils.write2json("data/nvd_update.json", updates)
