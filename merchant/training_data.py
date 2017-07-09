import bisect
import csv
import json
import logging

from kafka_downloader import download_kafka_files
from models.offer import Offer
from utils import extract_features


class TrainingData:
    """
    self.joined_data =
    {
        product_id: {
            timestamp: {
                "sales": [(timestamp, offer_id), (timestamp, offer_id), ...],
                merchant_id: {
                    offer_id: [ price, quality, ...]
                }
            }
        }
    }
    """

    def __init__(self, merchant_token, merchant_id,
                 market_situations_json=None, sales_json=None):
        self.joined_data = dict()
        self.merchant_token = merchant_token
        self.merchant_id = merchant_id
        self.timestamps = []
        self.last_sale_timestamp = None

    def update_timestamps(self):
        timestamps = set()
        for product in self.joined_data.values():
            for timestamp in product.keys():
                # TODO add at right position
                timestamps.add(timestamp)
        self.timestamps = sorted(timestamps)

    def create_training_data(self, product_id, interval_length=5):
        # self.update_timestamps()
        product = self.joined_data[product_id]
        sales_vector = []
        features_vector = []

        for timestamp, merchants in product.items():
            offer_list = []
            # [ [offer_id, price, quality] ]
            for merchant_id, offers in merchants.items():
                if merchant_id != 'sales':
                    for offer_id, attributes in offers.items():
                        # offer_list.append([offer_id, attributes[0], attributes[1]])
                        offer_list.append(Offer(offer_id, attributes[0], attributes[1]))

            if self.merchant_id in merchants:
                for offer_id in merchants[self.merchant_id].keys():
                    # sales_vector.append(self.extract_sales(product_id,
                    #                                        offer_id,
                    #                                        merchants.get('sales')))
                    # features_vector.append(extract_features(offer_id,
                    #                                              offer_list))
                    amount_sales = self.extract_sales(product_id, offer_id,
                                                      merchants.get('sales'))
                    features = extract_features(offer_id, offer_list)
                    if amount_sales == 0:
                        sales_vector.append(0)
                        features_vector.append(features)
                    else:
                        for i in range(amount_sales):
                            sales_vector.append(1)
                            features_vector.append(features)
        return (features_vector, sales_vector)

    def convert_training_data(self):
        converted = dict()
        for product_id in self.joined_data.keys():
            converted[product_id] = self.create_training_data(product_id)
        return converted

    def extract_sales(self, product_id, offer_id, sales):
        if not sales:
            return 0
        return [x[1] for x in sales].count(offer_id)

    def print_info(self):
        timestamps_ms = set()
        counter_ms = 0
        for product in self.market_situations.values():
            for timestamp, merchant in product.items():
                timestamps_ms.add(timestamp)
                for offer in merchant.values():
                    counter_ms += len(offer.keys())
        timestamps_s = set()
        counter_s = 0
        counter_s_same_timestamp = 0
        for product in self.sales.values():
            for timestamp, offers in product.items():
                timestamps_s.add(timestamp)
                counter_s += len(offers.keys())
                for o in offers.values():
                    if o > 1:
                        counter_s_same_timestamp += 1
        timestamps_ms = sorted(timestamps_ms)
        timestamps_s = sorted(timestamps_s)

        print('\nTraining data: \n\tEntries market_situations: {} \
               \n\tEntries sales: {} \
               \n\nmarket_situations: \
               \n\tDistinct timestamps: {} \
               \n\tFirst timestamp: {} \
               \n\tLast timestamp: {} \
               \n\nsales: \
               \n\tFirst timestamp: {} \
               \n\tLast timestamp: {} \
               \n\tMultiple sale events in one interval: {} \n\
               '.format(counter_ms, counter_s, len(timestamps_ms),
                        timestamps_ms[0], timestamps_ms[-1], timestamps_s[0],
                        timestamps_s[-1], counter_s_same_timestamp))

    def store_as_json(self):
        data = {'market_situations': self.market_situations,
                'sales': self.sales}
        with open('training_data.json', 'w') as fp:
            json.dump(data, fp)

    def append_marketplace_situations(self, line, csv_merchant_id=None):
        merchant_id = line['merchant_id']
        if csv_merchant_id == merchant_id:
            merchant_id = self.merchant_id

        if len(self.timestamps) > 0 and line['timestamp'] <= self.timestamps[-1]:
            return
        dict_keys = [line['product_id'], line['timestamp'], merchant_id]
        ms = self.joined_data
        for dk in dict_keys:
            if dk not in ms:
                ms[dk] = dict()
            ms = ms[dk]
        if line['offer_id'] not in ms:
            ms[line['offer_id']] = [float(line['price']), line['quality']]

    def append_sales(self, line):
        if self.last_sale_timestamp and line['timestamp'] <= self.last_sale_timestamp:
            return
        index = bisect.bisect(self.timestamps, line['timestamp']) - 1
        if index < 0:
            return
        timestamp = self.timestamps[index]
        self.last_sale_timestamp = line['timestamp']

        if timestamp not in self.joined_data[line['product_id']]:
            self.joined_data[line['product_id']][timestamp] = dict()

        interval = self.joined_data[line['product_id']][timestamp]
        if 'sales' in interval:
            interval['sales'].append((line['timestamp'], line['offer_id']))
        else:
            interval['sales'] = [(line['timestamp'], line['offer_id'])]

    def append_by_csvs(self, market_situations_path, buy_offer_path, csv_merchant_id=None):
        with open(market_situations_path, 'r') as csvfile:
            situation_data = csv.DictReader(csvfile)
            for line in situation_data:
                self.append_marketplace_situations(line, csv_merchant_id)
        self.update_timestamps()
        with open(buy_offer_path, 'r') as csvfile:
            buy_offer_data = csv.DictReader(csvfile)
            for line in buy_offer_data:
                self.append_sales(line)

    def append_by_kafka(self, market_situations_path=None, buy_offer_path=None):
        ########## use kafka example files
        if market_situations_path and buy_offer_path:
            self.append_by_csvs(market_situations_path, buy_offer_path)
            return
        #############

        try:
            ms, bo = download_kafka_files(self.merchant_token)

        except Exception as e:
            logging.warning('Could not download data from kafka: {}'.format(e))
            return
        if ms.status_code != 200 or bo.status_code != 200 or len(ms.text) < 10 or len(bo.text) < 10:
            logging.warning('Kafka download failed')
            return

        situation_data = csv.DictReader(ms.text.split('\n'))
        for line in situation_data:
            self.append_marketplace_situations(line)
        self.update_timestamps()
        buy_offer_data = csv.DictReader(bo.text.split('\n'))
        for line in buy_offer_data:
            self.append_sales(line)