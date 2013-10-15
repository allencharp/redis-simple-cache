"""
A simple redis-cache interface for storing python objects.
"""
from functools import wraps
import pickle
import json
import base64
import hashlib
import redis
import logging
import inspect
import types

class RedisConnect(object):
    '''
    A simple object to store and pass database connection information.
    This makes the Simple Cache class a little more flexible, for cases
    where redis connection configuration needs customizing.
    '''

    def __init__(self, host=None, port=None, db=None):
        self.host = host if host else 'localhost'
        self.port = port if port else 6379
        self.db = db if db else 0

    def connect(self):
        '''
        We cannot assume that connection will succeed, as such we use a ping()
        method in the redis client library to validate ability to contact redis.
        RedisNoConnException is raised if we fail to ping.
        '''
        try:
            redis.StrictRedis(host=self.host, port=self.port).ping()
        except redis.ConnectionError as e:
            raise RedisNoConnException, ("Failed to create connection to redis",
                                         (self.host,
                                          self.port)
                )
        return redis.StrictRedis(host=self.host, port=self.port, db=self.db)


class CacheMissException(Exception):
    pass


class ExpiredKeyException(Exception):
    pass


class RedisNoConnException(Exception):
    pass


class SimpleCache(object):

    def __init__(self, limit=1000, expire=60 * 60 * 24,
                 hashkeys=False, host=None, port=None, db=None, namespace=None):
        self.limit = limit  # No of json encoded strings to cache
        self.expire = expire  # Time to keys to expire in seconds

        self.unique = hash(inspect.currentframe().f_back.f_locals['__file__'])\
            if not namespace else namespace

        ## database number, host and port are optional, but passing them to
        ## RedisConnect object is best accomplished via optional arguments to
        ## the __init__ function upon instantiation of the class, instead of
        ## storing them in the class definition. Passing in None, which is a
        ## default already for database host or port will just assume use of
        ## Redis defaults.
        self.host = host
        self.port = port
        self.db = db
        ## We cannot assume that connection will always succeed. A try/except
        ## clause will assure unexpected behavior and an unhandled exception do not result.
        try:
            self.connection = RedisConnect(host=self.host, port=self.port, db=0).connect()
        except RedisNoConnException, e:
            self.connection = None
            pass

        ## There may be instances where we want to create hashes for
        ## keys to have a consistent length.
        self.hashkeys = hashkeys

    def make_key(self, key):
        return "{0}:{1}".format(self.unique, key)

    def get_set_name(self):
        return "{0}-keys".format(self.unique)

    def store(self, key, value, expire=None):
        """ Stores a value after checking for space constraints and freeing up space if required """
        key = to_unicode(key)
        value = to_unicode(value)
        set_name = self.get_set_name()

        while self.connection.scard(set_name) >= self.limit:
            del_key = self.connection.spop(set_name)
            self.connection.delete(self.make_key(del_key))

        pipe = self.connection.pipeline()
        if expire is None:
            expire = self.expire
        pipe.setex(self.make_key(key), expire, value)
        pipe.sadd(set_name, key)
        pipe.execute()

    def expire_all_in_set(self):
        """ Method expires all keys in the namespace of this object. At times there is
         a need to invalidate cache in bulk, because a single change may result
        in all data returned by a decorated function to be altered.
        Method returns a tuple where first value is total number of keys in the set of
        this object's namespace and second value is a number of keys successfully expired.
        :return: tuple(int, int)
        """
        self.set_name = self.get_set_name()
        self.all_members = self.keys()
        self.expired = 0
        for member in self.all_members:
            res = self.connection.expire("{0}:{1}".format(self.unique, member), 0)
            if res:
                self.expired += 1
        return self.__len__(), self.expired

    def isexpired(self, key):
        self.ttl = self.connection.pttl(key)
        if self.ttl == -1:
            return True
        if not self.ttl is None:
            return self.ttl
        else:
            return self.connection.pttl("{0}:{1}".format(self.unique, key))

    def store_json(self, key, value):
        self.store(key, json.dumps(value))

    def store_pickle(self, key, value):
        self.store(key, base64.b64encode(pickle.dumps(value)))

    def get(self, key):
        key = to_unicode(key)
        if key:  # No need to validate membership, which is an O(n) operation,
            value = self.connection.get(self.make_key(key))
            if value is None:  # expired key
                if not key in self:  # If key does not exist at all, it is a straight miss.
                    raise CacheMissException

                self.connection.srem(self.get_set_name(), key)
                raise ExpiredKeyException
            else:
                return value

    def get_json(self, key):
        return json.loads(self.get(key))

    def get_pickle(self, key):
        return pickle.loads(base64.b64decode(self.get(key)))

    def __contains__(self, key):
        """ Method establishes membership or lack thereof of a given key in this object's namespace.
        The obvious use case is with the `in` operator, i.e.: `key` in object.
        :param key: String representing a possible key in this object's namespace.
        :return: Boolean
        """
        return self.connection.sismember(self.get_set_name(), key)

    def __getitem__(self, item):
        """Select item from list of keys using array indexing. Single item is returned from
         list of all keys in the given set based on its position in the list.
        :param item: integer indicating location in the list.
        :return: string
        """
        if not isinstance(item, types.IntType):
            raise TypeError
        self.all_keys = list(self.connection.smembers(self.get_set_name()))
        return "{0}:{1}".format(self.unique, self.all_keys[item])

    def __iter__(self):
        """ Method returns an Iterator object producing individual keys from the this object's namespace.
        :return: iterator
        """
        if not self.connection:
            return iter([])
        return iter(["{0}:{1}".format(self.unique, x)
                    for x in self.connection.smembers(self.get_set_name())])

    def __len__(self):
        """ Return number of members in the given key namespace.
        :return: int
        """
        return self.connection.scard(self.get_set_name())

    def keys(self):
        return self.connection.smembers(self.get_set_name())

    def flush(self):
        keys = self.keys()
        pipe = self.connection.pipeline()
        for del_key in keys:
            pipe.delete(self.make_key(del_key))
        pipe.delete(self.get_set_name())
        pipe.execute()


def cache_it(limit=1000, expire=60 * 60 * 24, cache=None):
    """
    Apply this decorator to cache any function returning a value. Arguments and function result
    must be pickleable.
    """
    cache_ = cache  ## Since python 2.x doesn't have the nonlocal keyword, we need to do this
    def decorator(function):
        cache = cache_
        if cache is None:
            cache = SimpleCache(limit, expire, hashkeys=True)

        @wraps(function)
        def func(*args):
            ## Handle cases where caching is down or otherwise not available.
            if cache.connection is None:
                result = function(*args)
                return result

            ## Key will be either a md5 hash or just pickle object,
            ## in the form of `function name`:`key`
            if cache.hashkeys:
                key = hashlib.md5(pickle.dumps(args)).hexdigest()
            else:
                key = pickle.dumps(args)
            cache_key = '%s:%s' % (function.__name__, key)

            try:
                return cache.get_pickle(cache_key)
            except (ExpiredKeyException, CacheMissException) as e:
                ## Add some sort of cache miss handing here.
                pass
            except:
                logging.exception("Unknown redis-simple-cache error. Please check your Redis free space.")

            result = function(*args)
            cache.store_pickle(cache_key, result)
            return result
        return func
    return decorator


def cache_it_json(limit=1000, expire=60 * 60 * 24, cache=None):
    """
    A decorator similar to cache_it, but it serializes the return value to json, while storing
    in the database. Useful for types like list, tuple, dict, etc.
    """
    cache_ = cache  ## Since python 2.x doesn't have the nonlocal keyword, we need to do this
    def decorator(function):
        cache = cache_
        if cache is None:
            cache = SimpleCache(limit, expire, hashkeys=True)

        @wraps(function)
        def func(*args):
            ## Handle cases where caching is down or otherwise not available.
            if cache.connection is None:
                result = function(*args)
                return result

            ## Key will be either a md5 hash or just pickle object,
            ## in the form of `function name`:`key`
            if cache.hashkeys:
                key = hashlib.md5(json.dumps(args)).hexdigest()
            else:
                key = json.dumps(args)
            cache_key = '%s:%s' % (function.__name__, key)

            if cache_key in cache:
                try:
                    return cache.get_json(cache_key)
                except (ExpiredKeyException, CacheMissException) as e:
                    pass
                except:
                    logging.exception("Unknown redis-simple-cache error. Please check your Redis free space.")

            result = function(*args)
            cache.store_json(cache_key, result)
            return result
        return func
    return decorator


def to_unicode(obj, encoding='utf-8'):
    if isinstance(obj, basestring):
        if not isinstance(obj, unicode):
            obj = unicode(obj, encoding)
    return obj