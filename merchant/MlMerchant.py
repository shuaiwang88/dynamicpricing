import datetime
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import List

import numpy as np

from SuperMerchant import SuperMerchant
from merchant_sdk.models import Offer, Product
from training_data import TrainingData
from utils import extract_features
from utils import save_training_data, load_history


class MLMerchant(ABC, SuperMerchant):
    def __init__(self, settings):
        super().__init__(settings)
        if os.path.isfile(self.settings["data_file"]):
            self.machine_learning()
            self.last_learning = datetime.datetime.now()
        else:
            self.initial_learning()

        self.run_logic_loop()

    def initial_learning(self):
        self.training_data = TrainingData(self.merchant_token, self.merchant_id)
        self.training_data.append_by_csvs('../data/marketSituation.csv', '../data/buyOffer.csv',
                                          self.settings["initial_merchant_id"])
        # self.training_data.append_by_csvs('../data/ms1.csv', '../data/bo1.csv',
        #                                   self.settings["initial_merchant_id"])
        # self.training_data.append_by_csvs('../data/ms2.csv', '../data/bo2.csv',
        #                                   self.settings["initial_merchant_id"])

        save_training_data(self.training_data, self.settings["data_file"])
        self.train_model(self.training_data.convert_training_data())
        self.last_learning = datetime.datetime.now()

    def machine_learning(self):
        self.machine_learning_worker()
        # p = Process(target=self.machine_learning_worker)
        # p.start()
        # p.join()

    def machine_learning_worker(self):
        self.training_data = load_history(self.settings["data_file"])
        self.training_data.append_by_kafka()
        save_training_data(self.training_data, self.settings["data_file"])
        self.train_model(self.training_data.convert_training_data())
        self.last_learning = datetime.datetime.now()

    def execute_logic(self):
        self.perform_learning_if_necessary()
        request_count = 0

        # get and process existing offers
        offers = self.get_offers()
        own_offers = [offer for offer in offers if offer.merchant_id == self.merchant_id]
        own_offers_by_uid = {offer.uid: offer for offer in own_offers}
        missing_offers = self.settings["max_amount_of_offers"] - sum(offer.amount for offer in own_offers)

        # buy new products
        new_products = self.buy_new_products(missing_offers)
        product_prices_by_uid = self.get_product_prices()

        # handle bought products and either add them to existing offers or create new ones
        request_count = self.update_existing_offers(offers, own_offers, product_prices_by_uid, request_count)
        request_count = self.process_bought_products(new_products, offers, own_offers_by_uid, product_prices_by_uid, request_count)

        return max(1.0, request_count) / self.settings["max_req_per_sec"]

    def get_product_prices(self):
        try:
            products = self.producer_api.get_products()
            product_prices_by_uid = {product.uid: product.price for product in products}
            return product_prices_by_uid
        except Exception as e:
            logging.warning('Could not buy receive products from producer api: {}'.format(e))
        return dict()

    def process_bought_products(self, new_products: List[Product], offers: List[Offer], own_offers_by_uid: dict, product_prices_by_uid: dict, request_count: int):
        for product in new_products:
            try:
                if product.uid in own_offers_by_uid:
                    request_count = self.update_existing_offer(offers, own_offers_by_uid, product, product_prices_by_uid, request_count)
                else:
                    self.create_new_offer(offers, product, product_prices_by_uid)
            except Exception as e:
                print('could not handle product:', product, e)
        return request_count

    def create_new_offer(self, offers: List[Offer], product: Product, product_prices_by_uid: dict):
        offer = Offer.from_product(product)
        offer.prime = True
        offer.shipping_time['standard'] = self.settings["shipping"]
        offer.shipping_time['prime'] = self.settings["primeShipping"]
        offer.merchant_id = self.merchant_id
        offer.price = self.calculate_optimal_price(product_prices_by_uid, offer, current_offers=offers + [offer], product=product)
        try:
            self.marketplace_api.add_offer(offer)
        except Exception as e:
            print('error on adding an offer to the marketplace:', e)

    def update_existing_offer(self, offers: List[Offer], own_offers_by_uid: dict, product: Product, product_prices_by_uid: dict, request_count: int):
        offer = own_offers_by_uid[product.uid]
        offer.amount += product.amount
        offer.signature = product.signature
        try:
            self.marketplace_api.restock(offer.offer_id, amount=product.amount, signature=product.signature)
        except Exception as e:
            print('error on restocking an offer:', e)
        offer.price = self.calculate_optimal_price(product_prices_by_uid, offer, current_offers=offers, product=product)
        try:
            self.marketplace_api.update_offer(offer)
            request_count += 1
        except Exception as e:
            print('error on updating an offer:', e)
        return request_count

    def update_existing_offers(self, offers: List[Offer], own_offers: List[Offer], product_prices_by_uid: dict, request_count: int):
        for own_offer in own_offers:
            if own_offer.amount > 0:
                own_offer.price = self.calculate_optimal_price(product_prices_by_uid, own_offer, current_offers=offers)
                try:
                    self.marketplace_api.update_offer(own_offer)
                    request_count += 1
                except Exception as e:
                    logging.warning('Could not update offer on marketplace: {}'.format(e))
        return request_count

    def buy_new_products(self, missing_offers: int):
        new_products = []
        for _ in range(missing_offers):
            try:
                prod = self.producer_api.buy_product()
                new_products.append(prod)
            except Exception as e:
                logging.warning('Could not buy new product from producer api: {}'.format(e))
                raise e
        return new_products

    def get_offers(self):
        try:
            return self.marketplace_api.get_offers(include_empty_offers=False)
        except Exception as e:
            logging.warning('Could not receive offers from marketplace: {}'.format(e))
            raise e

    def perform_learning_if_necessary(self):
        next_training_session = self.last_learning + datetime.timedelta(minutes=self.settings["learning_interval"])
        if next_training_session <= datetime.datetime.now():
            self.last_learning = datetime.datetime.now()
            self.machine_learning()

    def calculate_optimal_price(self, product_prices_by_uid: dict, offer: Offer, current_offers: List[Offer] = None, product: Product = None):
        """
        Computes a price for a product based on trained models or (exponential) random fallback
        :param product: product object that is to be priced
        :param current_offers: list of offers
        :return:
        """

        price = product_prices_by_uid[offer.uid]
        if random.uniform(0, 1) < 0.3:
            return self.random_price(price)
        else:
            return self.ml_highest_profit(current_offers, offer, price)

    def ml_highest_profit(self, current_offers: List[Offer], offer: Offer, price: float):
        try:
            potential_prices = list(range(1, 100, 1))
            lst = self.create_prediction_data(offer, current_offers, potential_prices, price)

            probas = self.predict(str(offer.product_id), lst)
            expected_profits = self.calculate_expected_profits(potential_prices, price, probas)

            best_price = potential_prices[expected_profits.index(max(expected_profits))]
            print('.', end='')
            return best_price
        except (KeyError, ValueError, AttributeError):
            # Fallback for new porduct
            print('R', end='')
            return price * (np.random.exponential() + 0.99)

    def random_price(self, price: float):
        return (random.randint(price * 100, 10000) / 100)

    def calculate_expected_profits(self, potential_prices: List[float], price: float, probas: List):
        return [(proba * (potential_prices[i] - price)) for i, proba in enumerate(probas)]

    def create_prediction_data(self, own_offer: Offer, current_offers: List[Offer], potential_prices: List[int], price: float):
        lst = []
        for potential_price in potential_prices:
            potential_price_candidate = potential_price / 10.0
            potential_price = price + potential_price_candidate

            setattr(next(offer for offer in current_offers if offer.offer_id == own_offer.offer_id), "price", potential_price)
            prediction_data = extract_features(own_offer.offer_id, current_offers)
            lst.append(prediction_data)
        return lst

    @abstractmethod
    def train_model(self, features):
        pass

    @abstractmethod
    def predict(self, product_id, situations):
        pass
