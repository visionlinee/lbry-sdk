import asyncio
import struct
from binascii import hexlify, unhexlify
from decimal import Decimal
from operator import itemgetter
from typing import Optional, List, Iterable

from elasticsearch import AsyncElasticsearch, NotFoundError, ConnectionError
from elasticsearch.helpers import async_streaming_bulk

from lbry.crypto.base58 import Base58
from lbry.error import ResolveCensoredError, claim_id
from lbry.schema.result import Outputs, Censor
from lbry.schema.tags import clean_tags
from lbry.schema.url import URL, normalize_name
from lbry.utils import LRUCache
from lbry.wallet.server.db.common import CLAIM_TYPES, STREAM_TYPES
from lbry.wallet.server.util import class_logger


class SearchIndex:
    def __init__(self, index_prefix: str):
        self.client: Optional[AsyncElasticsearch] = None
        self.index = index_prefix + 'claims'
        self.sync_timeout = 600  # wont hit that 99% of the time, but can hit on a fresh import
        self.logger = class_logger(__name__, self.__class__.__name__)
        self.search_cache = LRUCache(2 ** 16)
        self.channel_cache = LRUCache(2 ** 16)

    async def start(self):
        if self.client:
            return
        self.client = AsyncElasticsearch(timeout=self.sync_timeout)
        while True:
            try:
                await self.client.cluster.health(wait_for_status='yellow')
                break
            except ConnectionError:
                self.logger.warning("Failed to connect to Elasticsearch. Waiting for it!")
                await asyncio.sleep(1)
        res = await self.client.indices.create(
            self.index,
            {
                "settings":
                    {"analysis":
                        {"analyzer": {
                            "default": {"tokenizer": "whitespace", "filter": ["lowercase", "porter_stem"]}}},
                        "index":
                            {"refresh_interval": -1,
                             "number_of_shards": 1,
                             "number_of_replicas": 0}
                    },
                "mappings": {
                    "properties": {
                        "claim_id": {
                            "fields": {
                                "keyword": {
                                    "ignore_above": 256,
                                    "type": "keyword"
                                }
                            },
                            "type": "text",
                            "index_prefixes": {
                                "min_chars": 1,
                                "max_chars": 10
                            }
                        },
                        "height": {"type": "integer"},
                        "claim_type": {"type": "byte"},
                        "censor_type": {"type": "byte"},
                        "trending_mixed": {"type": "float"},
                    }
                }
            }, ignore=400
        )
        return res.get('acknowledged', False)

    def stop(self):
        client = self.client
        self.client = None
        return asyncio.ensure_future(client.close())

    def delete_index(self):
        return self.client.indices.delete(self.index, ignore_unavailable=True)

    async def _queue_consumer_doc_producer(self, queue: asyncio.Queue):
        while not queue.empty():
            op, doc = queue.get_nowait()
            if op == 'delete':
                yield {'_index': self.index, '_op_type': 'delete', '_id': doc}
            else:
                yield extract_doc(doc, self.index)

    async def sync_queue(self, claim_queue):
        self.logger.info("Writing to index from a queue with %d elements.", claim_queue.qsize())
        await self.client.indices.refresh(self.index)
        async for ok, item in async_streaming_bulk(self.client, self._queue_consumer_doc_producer(claim_queue)):
            if not ok:
                self.logger.warning("indexing failed for an item: %s", item)
        await self.client.indices.refresh(self.index)
        await self.client.indices.flush(self.index)
        self.logger.info("Indexing done. Queue: %d elements", claim_queue.qsize())
        self.search_cache.clear()
        self.channel_cache.clear()

    async def apply_filters(self, blocked_streams, blocked_channels, filtered_streams, filtered_channels):
        def make_query(censor_type, blockdict, channels=False):
            blockdict = dict(
                (hexlify(key[::-1]).decode(), hexlify(value[::-1]).decode()) for key, value in blockdict.items())
            if channels:
                update = expand_query(channel_id__in=list(blockdict.keys()), censor_type=f"<{censor_type}")
            else:
                update = expand_query(claim_id__in=list(blockdict.keys()), censor_type=f"<{censor_type}")
            key = 'channel_id' if channels else 'claim_id'
            update['script'] = {
                "source": f"ctx._source.censor_type={censor_type}; ctx._source.censoring_channel_hash=params[ctx._source.{key}]",
                "lang": "painless",
                "params": blockdict
            }
            return update
        if filtered_streams:
            await self.client.update_by_query(self.index, body=make_query(1, filtered_streams), slices=32)
            await self.client.indices.refresh(self.index)
        if filtered_channels:
            await self.client.update_by_query(self.index, body=make_query(1, filtered_channels), slices=32)
            await self.client.indices.refresh(self.index)
            await self.client.update_by_query(self.index, body=make_query(1, filtered_channels, True), slices=32)
            await self.client.indices.refresh(self.index)
        if blocked_streams:
            await self.client.update_by_query(self.index, body=make_query(2, blocked_streams), slices=32)
            await self.client.indices.refresh(self.index)
        if blocked_channels:
            await self.client.update_by_query(self.index, body=make_query(2, blocked_channels), slices=32)
            await self.client.indices.refresh(self.index)
            await self.client.update_by_query(self.index, body=make_query(2, blocked_channels, True), slices=32)
            await self.client.indices.refresh(self.index)

    async def delete_above_height(self, height):
        await self.client.delete_by_query(self.index, expand_query(height='>'+str(height)))
        await self.client.indices.refresh(self.index)

    async def session_query(self, query_name, kwargs):
        offset, total = kwargs.get('offset', 0) if isinstance(kwargs, dict) else 0, 0
        total_referenced = []
        if query_name == 'resolve':
            total_referenced, response, censor = await self.resolve(*kwargs)
        else:
            censor = Censor(Censor.SEARCH)
            response, offset, total = await self.search(**kwargs)
            censor.apply(response)
            total_referenced.extend(response)
            if censor.censored:
                response, _, _ = await self.search(**kwargs, censor_type=0)
                total_referenced.extend(response)
        return Outputs.to_base64(response, await self._get_referenced_rows(total_referenced), offset, total, censor)

    async def resolve(self, *urls):
        censor = Censor(Censor.RESOLVE)
        results = [await self.resolve_url(url) for url in urls]
        censored = [
            result if not isinstance(result, dict) or not censor.censor(result)
            else ResolveCensoredError(url, result['censoring_channel_hash'])
            for url, result in zip(urls, results)
        ]
        return results, censored, censor

    async def get_many(self, *claim_ids):
        cached = {claim_id: self.search_cache.get(claim_id) for claim_id in claim_ids if claim_id in self.search_cache}
        missing = [claim_id for claim_id in claim_ids if claim_id not in cached]
        if missing:
            results = await self.client.mget(index=self.index, body={"ids": missing},
                                             _source_excludes=['description', 'title'])
            results = expand_result(filter(lambda doc: doc['found'], results["docs"]))
            for result in results:
                self.search_cache.set(result['claim_id'], result)
        return list(filter(None, map(self.search_cache.get, claim_ids)))

    async def search(self, **kwargs):
        if 'channel' in kwargs:
            result = await self.resolve_url(kwargs.pop('channel'))
            if not result or not isinstance(result, Iterable):
                return [], 0, 0
            kwargs['channel_id'] = result['claim_id']
        try:
            result = await self.client.search(expand_query(**kwargs), index=self.index)
        except NotFoundError:
            # index has no docs, fixme: log something
            return [], 0, 0
        return expand_result(result['hits']['hits']), 0, result['hits']['total']['value']

    async def resolve_url(self, raw_url):
        try:
            url = URL.parse(raw_url)
        except ValueError as e:
            return e

        stream = LookupError(f'Could not find claim at "{raw_url}".')

        channel_id = await self.resolve_channel_id(url)
        if isinstance(channel_id, LookupError):
            return channel_id
        stream = (await self.resolve_stream(url, channel_id if isinstance(channel_id, str) else None)) or stream
        if url.has_stream:
            result = stream
        else:
            if isinstance(channel_id, str):
                result = (await self.get_many(channel_id))
                result = result[0] if len(result) else LookupError(f'Could not find channel in "{url}".')
            else:
                result = channel_id

        return result

    async def resolve_channel_id(self, url: URL):
        if not url.has_channel:
            return
        key = 'cid:' + str(url.channel)
        if key in self.channel_cache:
            return self.channel_cache[key]
        query = url.channel.to_dict()
        if set(query) == {'name'}:
            query['is_controlling'] = True
        else:
            query['order_by'] = ['^creation_height']
        if len(query.get('claim_id', '')) != 40:
            matches, _, _ = await self.search(**query, limit=1)
            if matches:
                channel_id = matches[0]['claim_id']
            else:
                return LookupError(f'Could not find channel in "{url}".')
        else:
            channel_id = query['claim_id']
        self.channel_cache.set(key, channel_id)
        return channel_id

    async def resolve_stream(self, url: URL, channel_id: str = None):
        if not url.has_stream:
            return None
        if url.has_channel and channel_id is None:
            return None
        query = url.stream.to_dict()
        stream = None
        if 'claim_id' in query and len(query['claim_id']) == 40:
            stream = (await self.get_many(query['claim_id']))
            stream = stream[0] if len(stream) else None
        else:
            key = (channel_id or '') + str(url.stream)
            if key in self.search_cache:
                return self.search_cache[key]
        if channel_id is not None:
            if set(query) == {'name'}:
                # temporarily emulate is_controlling for claims in channel
                query['order_by'] = ['effective_amount', '^height']
            else:
                query['order_by'] = ['^channel_join']
            query['channel_id'] = channel_id
            query['signature_valid'] = True
        elif set(query) == {'name'}:
            query['is_controlling'] = True
        if not stream:
            matches, _, _ = await self.search(**query, limit=1)
            if matches:
                stream = matches[0]
                key = (channel_id or '') + str(url.stream)
                self.search_cache.set(key, stream)
        return stream

    async def _get_referenced_rows(self, txo_rows: List[dict]):
        txo_rows = [row for row in txo_rows if isinstance(row, dict)]
        repost_hashes = set(filter(None, map(itemgetter('reposted_claim_id'), txo_rows)))
        channel_hashes = set(filter(None, (row['channel_id'] for row in txo_rows)))
        channel_hashes |= set(map(claim_id, filter(None, (row['censoring_channel_hash'] for row in txo_rows))))

        reposted_txos = []
        if repost_hashes:
            reposted_txos = await self.get_many(*repost_hashes)
            channel_hashes |= set(filter(None, (row['channel_id'] for row in reposted_txos)))

        channel_txos = []
        if channel_hashes:
            channel_txos = await self.get_many(*channel_hashes)

        # channels must come first for client side inflation to work properly
        return channel_txos + reposted_txos


def extract_doc(doc, index):
    doc['claim_id'] = hexlify(doc.pop('claim_hash')[::-1]).decode()
    if doc['reposted_claim_hash'] is not None:
        doc['reposted_claim_id'] = hexlify(doc.pop('reposted_claim_hash')[::-1]).decode()
    else:
        doc['reposted_claim_id'] = None
    channel_hash = doc.pop('channel_hash')
    doc['channel_id'] = hexlify(channel_hash[::-1]).decode() if channel_hash else channel_hash
    channel_hash = doc.pop('censoring_channel_hash')
    doc['censoring_channel_hash'] = hexlify(channel_hash[::-1]).decode() if channel_hash else channel_hash
    txo_hash = doc.pop('txo_hash')
    doc['tx_id'] = hexlify(txo_hash[:32][::-1]).decode()
    doc['tx_nout'] = struct.unpack('<I', txo_hash[32:])[0]
    doc['is_controlling'] = bool(doc['is_controlling'])
    doc['signature'] = hexlify(doc.pop('signature') or b'').decode() or None
    doc['signature_digest'] = hexlify(doc.pop('signature_digest') or b'').decode() or None
    doc['public_key_bytes'] = hexlify(doc.pop('public_key_bytes') or b'').decode() or None
    doc['public_key_hash'] = hexlify(doc.pop('public_key_hash') or b'').decode() or None
    doc['signature_valid'] = bool(doc['signature_valid'])
    doc['claim_type'] = doc.get('claim_type', 0) or 0
    doc['stream_type'] = int(doc.get('stream_type', 0) or 0)
    return {'doc': doc, '_id': doc['claim_id'], '_index': index, '_op_type': 'update',
           'doc_as_upsert': True}


FIELDS = {'is_controlling', 'last_take_over_height', 'claim_id', 'claim_name', 'normalized', 'tx_position', 'amount',
          'timestamp', 'creation_timestamp', 'height', 'creation_height', 'activation_height', 'expiration_height',
          'release_time', 'short_url', 'canonical_url', 'title', 'author', 'description', 'claim_type', 'reposted',
          'stream_type', 'media_type', 'fee_amount', 'fee_currency', 'duration', 'reposted_claim_hash', 'censor_type',
          'claims_in_channel', 'channel_join', 'signature_valid', 'effective_amount', 'support_amount',
          'trending_group', 'trending_mixed', 'trending_local', 'trending_global', 'channel_id', 'tx_id', 'tx_nout',
          'signature', 'signature_digest', 'public_key_bytes', 'public_key_hash', 'public_key_id', '_id', 'tags',
          'reposted_claim_id'}
TEXT_FIELDS = {'author', 'canonical_url', 'channel_id', 'claim_name', 'description', 'claim_id',
               'media_type', 'normalized', 'public_key_bytes', 'public_key_hash', 'short_url', 'signature',
               'signature_digest', 'stream_type', 'title', 'tx_id', 'fee_currency', 'reposted_claim_id', 'tags'}
RANGE_FIELDS = {
    'height', 'creation_height', 'activation_height', 'expiration_height',
    'timestamp', 'creation_timestamp', 'duration', 'release_time', 'fee_amount',
    'tx_position', 'channel_join', 'reposted', 'limit_claims_per_channel',
    'amount', 'effective_amount', 'support_amount',
    'trending_group', 'trending_mixed', 'censor_type',
    'trending_local', 'trending_global',
}
REPLACEMENTS = {
    'name': 'normalized',
    'txid': 'tx_id',
    'claim_hash': '_id'
}


def expand_query(**kwargs):
    if "amount_order" in kwargs:
        kwargs["limit"] = 1
        kwargs["order_by"] = "effective_amount"
        kwargs["offset"] = int(kwargs["amount_order"]) - 1
    if 'name' in kwargs:
        kwargs['name'] = normalize_name(kwargs.pop('name'))
    if kwargs.get('is_controlling') is False:
        kwargs.pop('is_controlling')
    query = {'must': [], 'must_not': []}
    collapse = None
    for key, value in kwargs.items():
        key = key.replace('claim.', '')
        many = key.endswith('__in') or isinstance(value, list)
        if many:
            key = key.replace('__in', '')
            value = list(filter(None, value))
        if value is None or isinstance(value, list) and len(value) == 0:
            continue
        key = REPLACEMENTS.get(key, key)
        if key in FIELDS:
            partial_id = False
            if key == 'claim_type':
                if isinstance(value, str):
                    value = CLAIM_TYPES[value]
                else:
                    value = [CLAIM_TYPES[claim_type] for claim_type in value]
            if key == '_id':
                if isinstance(value, Iterable):
                    value = [hexlify(item[::-1]).decode() for item in value]
                else:
                    value = hexlify(value[::-1]).decode()
            if not many and key in ('_id', 'claim_id') and len(value) < 20:
                partial_id = True
            if key == 'public_key_id':
                key = 'public_key_hash'
                value = hexlify(Base58.decode(value)[1:21]).decode()
            if key == 'signature_valid':
                continue  # handled later
            if key in TEXT_FIELDS:
                key += '.keyword'
            ops = {'<=': 'lte', '>=': 'gte', '<': 'lt', '>': 'gt'}
            if partial_id:
                query['must'].append({"prefix": {"claim_id": value}})
            elif key in RANGE_FIELDS and isinstance(value, str) and value[0] in ops:
                operator_length = 2 if value[:2] in ops else 1
                operator, value = value[:operator_length], value[operator_length:]
                if key == 'fee_amount':
                    value = Decimal(value)*1000
                query['must'].append({"range": {key: {ops[operator]: value}}})
            elif many:
                query['must'].append({"terms": {key: value}})
            else:
                if key == 'fee_amount':
                    value = Decimal(value)*1000
                query['must'].append({"term": {key: {"value": value}}})
        elif key == 'not_channel_ids':
            for channel_id in value:
                query['must_not'].append({"term": {'channel_id.keyword': channel_id}})
                query['must_not'].append({"term": {'_id': channel_id}})
        elif key == 'channel_ids':
            query['must'].append({"terms": {'channel_id.keyword': value}})
        elif key == 'claim_ids':
            query['must'].append({"terms": {'claim_id.keyword': value}})
        elif key == 'media_types':
            query['must'].append({"terms": {'media_type.keyword': value}})
        elif key == 'stream_types':
            query['must'].append({"terms": {'stream_type': [STREAM_TYPES[stype] for stype in value]}})
        elif key == 'any_languages':
            query['must'].append({"terms": {'languages': clean_tags(value)}})
        elif key == 'any_languages':
            query['must'].append({"terms": {'languages': value}})
        elif key == 'all_languages':
            query['must'].extend([{"term": {'languages': tag}} for tag in value])
        elif key == 'any_tags':
            query['must'].append({"terms": {'tags.keyword': clean_tags(value)}})
        elif key == 'all_tags':
            query['must'].extend([{"term": {'tags.keyword': tag}} for tag in clean_tags(value)])
        elif key == 'not_tags':
            query['must_not'].extend([{"term": {'tags.keyword': tag}} for tag in clean_tags(value)])
        elif key == 'not_claim_id':
            query['must_not'].extend([{"term": {'claim_id.keyword': cid}} for cid in value])
        elif key == 'limit_claims_per_channel':
            collapse = ('channel_id.keyword', value)
    if kwargs.get('has_channel_signature'):
        query['must'].append({"exists": {"field": "signature_digest"}})
        if 'signature_valid' in kwargs:
            query['must'].append({"term": {"signature_valid": bool(kwargs["signature_valid"])}})
    elif 'signature_valid' in kwargs:
        query.setdefault('should', [])
        query["minimum_should_match"] = 1
        query['should'].append({"bool": {"must_not": {"exists": {"field": "signature_digest"}}}})
        query['should'].append({"term": {"signature_valid": bool(kwargs["signature_valid"])}})
    if kwargs.get('text'):
        query['must'].append(
                    {"simple_query_string":
                         {"query": kwargs["text"], "fields": [
                             "claim_name^4", "channel_name^8", "title^1", "description^.5", "author^1", "tags^.5"
                         ]}})
    query = {
        "_source": {"excludes": ["description", "title"]},
        'query': {'bool': query},
        "sort": [],
    }
    if "limit" in kwargs:
        query["size"] = kwargs["limit"]
    if 'offset' in kwargs:
        query["from"] = kwargs["offset"]
    if 'order_by' in kwargs:
        if isinstance(kwargs["order_by"], str):
            kwargs["order_by"] = [kwargs["order_by"]]
        for value in kwargs['order_by']:
            if 'trending_group' in value:
                # fixme: trending_mixed is 0 for all records on variable decay, making sort slow.
                continue
            is_asc = value.startswith('^')
            value = value[1:] if is_asc else value
            value = REPLACEMENTS.get(value, value)
            if value in TEXT_FIELDS:
                value += '.keyword'
            query['sort'].append({value: "asc" if is_asc else "desc"})
    if collapse:
        query["collapse"] = {
            "field": collapse[0],
            "inner_hits": {
                "name": collapse[0],
                "size": collapse[1],
                "sort": query["sort"]
            }
        }
    return query


def expand_result(results):
    inner_hits = []
    expanded = []
    for result in results:
        if result.get("inner_hits"):
            for _, inner_hit in result["inner_hits"].items():
                inner_hits.extend(inner_hit["hits"]["hits"])
            continue
        result = result['_source']
        result['claim_hash'] = unhexlify(result['claim_id'])[::-1]
        if result['reposted_claim_id']:
            result['reposted_claim_hash'] = unhexlify(result['reposted_claim_id'])[::-1]
        else:
            result['reposted_claim_hash'] = None
        result['channel_hash'] = unhexlify(result['channel_id'])[::-1] if result['channel_id'] else None
        result['txo_hash'] = unhexlify(result['tx_id'])[::-1] + struct.pack('<I', result['tx_nout'])
        result['tx_hash'] = unhexlify(result['tx_id'])[::-1]
        if result['censoring_channel_hash']:
            result['censoring_channel_hash'] = unhexlify(result['censoring_channel_hash'])[::-1]
        expanded.append(result)
    if inner_hits:
        return expand_result(inner_hits)
    return expanded
