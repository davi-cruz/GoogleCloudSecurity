import functions_framework

"""Fetch data from Imperva Cloud WAF"""

import sys
import requests
from requests.packages.urllib3.util.retry import Retry
import urllib3
import zlib

from common import ingest
from common import utils

from google.cloud import storage
from google.cloud.exceptions import NotFound

# Environment variable constants.
imperva_waf_api_id = utils.get_env_var("IMPERVA_WAF_API_ID")
imperva_waf_api_secret = utils.get_env_var("IMPERVA_WAF_API_SECRET", is_secret=True)
imperva_waf_logserver_uri = utils.get_env_var("IMPERVA_WAF_TARGET_SERVER")
logs_encryption_private_key = utils.get_env_var("IMPERVA_WAF_PRIVATE_KEY", is_secret=True)
chronicle_data_type = "IMPERVA_WAF"

bucket_name = utils.get_env_var("GCS_BUCKET_NAME")
file_path = utils.get_env_var("STATE_FILE_PATH")

class StateManager:
    def __init__(self, bucket_name: str, file_path: str):
        self.bucket_name = bucket_name
        self.file_path = file_path
        self.storage_client = storage.Client()
        self.bucket = self.storage_client.bucket(self.bucket_name)

    def post(self, marker_text: str):
        blob = self.bucket.blob(self.file_path)
        blob.upload_from_string(marker_text)

    def get(self):
        try:
            blob = self.bucket.blob(self.file_path)
            return blob.download_as_string().decode()
        except NotFound:
            return None

class ImpervaFilesHandler:
    def __init__(self):
        self.url = imperva_waf_logserver_uri
        retries = Retry(
            total=3,
            status_forcelist={500, 429},
            backoff_factor=1,
            respect_retry_after_header=True
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retries)
        self.session = requests.Session()
        self.session.mount('https://', adapter)
        self.auth = urllib3.make_headers(basic_auth='{}:{}'.format(
            imperva_waf_api_id, imperva_waf_api_secret))
        self.files_array = self.list_index_file()

    def list_index_file(self):
        files_array = []
        try:
            r = self.session.get(url="{}/{}".format(self.url, f"logs.index"),
                                 headers=self.auth
                                 )
            if 200 <= r.status_code <= 299:
                print("Successfully downloaded index file.")
                for line in r.iter_lines():
                    files_array.append(line.decode('UTF-8'))
                return files_array
            elif r.status_code == 400:
                print("Bad Request. The request was invalid or cannot be otherwise served."
                      " Error code: {}".format(r.status_code), file=sys.stderr)
            elif r.status_code == 404:
                print(
                    "Could not find index file. Response code is {}".format(r.status_code), file=sys.stderr)
            elif r.status_code == 401:
                print(
                    "Authorization error - Failed to download index file. Response code is {}".format(r.status_code), file=sys.stderr)
            elif r.status_code == 429:
                print(
                    "Rate limit exceeded - Failed to download index file. Response code is {}".format(r.status_code), file=sys.stderr)
            else:
                if r.status_code is None:
                    print(
                        "Something wrong. Error text: {}".format(r.text), file=sys.stderr)
                else:
                    print(
                        "Something wrong. Error code: {}".format(r.status_code), file=sys.stderr)
        except Exception as err:
            print(
                "Something wrong. Exception error text: {}".format(err), file=sys.stderr)

    def last_file_point(self):
        try:
            if self.files_array is not None:
                state = StateManager(bucket_name, file_path)
                past_file = state.get()
                if past_file is not None:
                    print(
                        "The last file point is: {}".format(past_file))
                    try:
                        index = self.files_array.index(past_file)
                        files_arr = self.files_array[index + 1:]
                    except Exception as err:
                        print(
                            "Last point file detection error: {}. So Processing all the files from index file".format(err))
                        files_arr = self.files_array
                else:
                    files_arr = self.files_array
                print(
                    "There are {} files in the list index file.".format(len(files_arr)))
                if self.files_array is not None:
                    current_file = self.files_array[-1]
                state.post(current_file)
                return files_arr
        except Exception as err:
            print(
                "Last point file detection error. Exception error text: {}".format(err), file=sys.stderr)

    def download_files(self):
        files_for_download = self.last_file_point()
        if files_for_download is not None:
            for file in files_for_download:
                print("Downloading file {}".format(file))
                self.download_file(file)

    def download_file(self, file_name):
        try:
            r = self.session.get(
                url="{}/{}".format(self.url, file_name), stream=True, headers=self.auth)
            if 200 <= r.status_code <= 299:
                print(
                    "Successfully downloaded file: {}".format(file_name))
                self.decrypt_and_unpack_file(file_name, r.content)
                return r.status_code
            elif r.status_code == 400:
                print("Bad Request. The request was invalid or cannot be otherwise served."
                      " Error code: {}".format(r.status_code), file=sys.stderr)
            elif r.status_code == 404:
                print("Could not find file {}. Response code: {}".format(
                    file_name, r.status_code), file=sys.stderr)
            elif r.status_code == 401:
                print(
                    "Authorization error - Failed to download file {}. Response code: {}".format(file_name, r.status_code), file=sys.stderr)
            elif r.status_code == 429:
                print(
                    "Rate limit exceeded - Failed to downloadfile {}. Response code: {}".format(file_name, r.status_code), file=sys.stderr)
            else:
                if r.status_code is None:
                    print(
                        "Something wrong. Error text: {}".format(r.text), file=sys.stderr)
                else:
                    print(
                        "Something wrong. Error code: {}".format(r.status_code), file=sys.stderr)
        except Exception as err:
            print(
                "Something wrong. Exception error text: {}".format(err), file=sys.stderr)

    def decrypt_and_unpack_file(self, file_name, file_content):
        print("Unpacking and decrypting file {}".format(file_name))
        file_splitted = file_content.split(b"|==|\n")
        file_header = file_splitted[0].decode("utf-8")
        file_data = file_splitted[1]
        file_encryption_flag = file_header.find("key:")
        events_arr = []
        if file_encryption_flag == -1:
            try:
                events_data = zlib.decompressobj().decompress(file_data).decode("utf-8")
            except Exception as err:
                if 'while decompressing data: incorrect header check' in err.args[0]:
                    events_data = file_data.decode("utf-8")
                else:
                    print(
                        "Error during decompressing and decoding the file with error message {}.".format(err), file=sys.stderr)
        if events_data is not None:
            for line in events_data.splitlines():
                events_arr.append(line)
        ingest.ingest(events_arr, chronicle_data_type)

    def gen_chunks_to_object(self, object, chunksize=100):
        chunk = []
        for index, line in enumerate(object):
            if (index % chunksize == 0 and index > 0):
                yield chunk
                del chunk[:]
            chunk.append(line)
        yield chunk

@functions_framework.http
def main(req):
    """Entrypoint

    Args:
        req: Request to execute the cloud function.

    Returns:
        string: "Ingestion completed."
    """

    print('Starting program')
    ifh = ImpervaFilesHandler()
    ifh.download_files()

    return "Ingestion completed."
