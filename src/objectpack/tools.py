#coding: utf-8
"""
Created on 23.07.2012
@author: pirogov
"""
import datetime
from functools import wraps
from operator import attrgetter as _attrgetter

from django.db import transaction as _transaction
from django.db.models.fields.related import RelatedField as _RelatedField


#==============================================================================
# QuerySplitter
#==============================================================================
class QuerySplitter(object):
    """
    Порционный загрузчик выборки в итеративном контексте

    >>> from django.test.client import RequestFactory
    >>> rf = RequestFactory()
    >>> request = rf.post('', {'start': 5, 'limit': 10})
    >>> QuerySplitter.make_rows(
    ...     query=range(50),
    ...     validator=lambda x: x % 2,
    ...     request=request)
    [5, 7, 9, 11, 13, 15, 17, 19, 21, 23]
    """

    def __init__(self, query, start, limit=0):
        """
        query - выборка, start и limit - откуда и сколько отрезать.
        """
        self._data = query
        self._start = start
        self._limit = limit

        self._chunk = None
        self._cnt = 0
        self._no_more_data = False

    def __iter__(self):
        if not self._limit:
            # перекрытие метода пропускания, заглушкой
            self.skip_last = lambda self: None
            return iter(self._data)
        return self

    def next(self):
        # если уже выдали нужное кол-во, останавливаем итерацию
        if self._cnt >= self._limit:
            raise StopIteration()

        # если порция кончилась, берем следующую
        if not self._chunk and not self._no_more_data:
            self._chunk = list(
                self._data[self._start: self._start + self._limit])
            if len(self._chunk) < self._limit:
                self._no_more_data = True
            else:
                self._start += self._limit

        # отдаём порцию поэлементно
        if self._chunk:
            self._cnt += 1
            return self._chunk.pop(0)

        raise StopIteration()

    def skip_last(self):
        """
        Команда "не учитывать прошлое значение"
        """
        if not self._cnt:
            raise IndexError('Can`t skip any more!')
        self._cnt -= 1

    @classmethod
    def make_rows(
            cls, query,
            row_fabric=lambda item: item,
            validator=lambda item: True,
            request=None, start=0, limit=25):
        """
        Формирует список элементов для грида из выборки.
        Параметры листания берутся из :attr:`request`,
        или из параметров :attr:`start`/:attr:`limit`.
        Элементы перед попаданием прогоняются через :attr:`row_fabric`.
        В результирующий список попадают только те элементы,
        вызов :attr:`validator` для которых возвращает `True`

        :param query: Кварисет
        :type query: django.db.models.query.QuerySet
        :param row_fabric:
        :type row_fabric: types.FunctionType
        :param validator: Функция валидатор
        :type validator: types.FunctionType
        :param request: Реквест
        :type request: django.http.HttpRequest
        :param start: С какой записи начинать
        :type start: int
        :param limit: Сколько записей взять
        :type limit: int
        """
        if request:
            start = extract_int(request, 'start') or start
            limit = extract_int(request, 'limit') or limit

        query = cls(query, start, limit)

        rows = []
        for item in query:
            if validator(item):
                rows.append(row_fabric(item))
            else:
                query.skip_last()
        return rows


#==============================================================================
# ModelCache
#==============================================================================
class ModelCache(object):
    """
    Кэш get-ов объектов одной модели.
    В качестве ключа кэша - набор параметров для get-а
    Если в конструкторе указана фабрика объектов, то отсутствующие объекты
    создаются передачей аргументов фабрике.
    """

    def __init__(self, model, object_fabric=None):
        self._model = model
        self.MultipleObjectsReturned = model.MultipleObjectsReturned
        self._cache = {}
        self._last_kwargs = {}
        self._fabric = object_fabric

    @staticmethod
    def _key_for_dict(d):
        return tuple(sorted(d.iteritems(), key=lambda i: i[0]))

    def _get_object(self, kwargs):
        try:
            return self._model.objects.get(**kwargs)
        except self._model.DoesNotExist:
            return None

    def get(self, **kwargs):
        self._last_kwargs = kwargs

        key = self._key_for_dict(kwargs)

        if key in self._cache:
            return self._cache[key]

        new = self._get_object(kwargs)

        if new is None and self._fabric:
            new = self._fabric(**kwargs)
            assert isinstance(new, self._model)
            assert not new.id is None

        self._cache[key] = new

        return new

    def forget_last(self):
        if self._last_kwargs:
            key = self._key_for_dict(self._last_kwargs)
            self._cache.pop(key, None)


#==============================================================================
# TransactionCM
#==============================================================================
class TransactionCM(object):
    """
    Транизакция в виде ContextManager
    """
    def __init__(self, using=None, catcher=None):
        """
        using - DB alias
        catcher - внешний обработчик исключений
        """
        self._using = using
        self._catcher = catcher or self._default_catcher

    def __enter__(self):
        _transaction.enter_transaction_management(True, self._using)
        return _transaction

    def __exit__(self, *args):
        result = self._catcher(*args)
        if result:
            _transaction.commit(self._using)
        else:
            _transaction.rollback(self._using)
        return result

    @staticmethod
    def _default_catcher(ex_type, *args):
        """
        Обработчик исключений, используемый по-умолчанию
        """
        return ex_type is None


def extract_int(request, key):
    """
    Нормальный извлекатель числа

    >>> from django.test.client import RequestFactory
    >>> rf = RequestFactory()
    >>> request = rf.post('', {})
    >>> extract_int(request, 'NaN')

    >>> request = rf.post('', {'int':1})
    >>> extract_int(request, 'int')
    1
    """
    try:
        return int(request.REQUEST.get(key, ''))
    except ValueError:
        return None


def extract_int_list(request, key):
    """
    Нормальный извлекатель списка чисел

    >>> from django.test.client import RequestFactory
    >>> rf = RequestFactory()
    >>> request = rf.post('', {})
    >>> extract_int_list(request, 'list')
    []

    >>> request = rf.post('', {'list':'1,2,3,4'})
    >>> extract_int_list(request, 'list')
    [1, 2, 3, 4]
    """
    return map(int, filter(None, request.REQUEST.get(key, '').split(',')))


def str_to_date(raw_str):
    """
    Извлечение даты из строки

    >>> str_to_date('31.12.2012') == str_to_date('2012-12-31, Happy New Year')
    True
    """
    if raw_str:
        raw_str = raw_str[:10]
        raw_str = raw_str.replace('-', '.')
        try:
            raw_str = datetime.datetime.strptime(raw_str, '%d.%m.%Y')
        except ValueError:
            try:
                raw_str = datetime.datetime.strptime(raw_str, '%Y.%m.%d')
            except ValueError:
                raw_str = None
    else:
        raw_str = None
    return raw_str


def extract_date(request, key, as_date=False):
    """
    Извлечение даты из request`а в формате DD.MM.YYYY
    (в таком виде приходит от ExtDateField)
    и приведение к Django-формату (YYYY-MM-DD)
    """
    res = str_to_date(request.REQUEST.get(key))
    if res and as_date:
        res = res.date()
    return res


def modify(obj, **kwargs):
    """
    Массовое дополнение атрибутов для объекта с его (объекта) возвратом

    >>> class Object(object): pass
    >>> cls = Object()
    >>> cls.param1 = 0
    >>> cls = modify(cls, **{'param1':1, })
    >>> cls.param1
    1
    """
    for attr, val in kwargs.iteritems():
        setattr(obj, attr, val)
    return obj


def modifier(**kwargs):
    """
    Принимает атрибуты со значениями (в виде kwargs)
    Возвращает модификатор - функцию, модифицирующую передаваемый ей объект
    указанными атрибутами

    >>> w10 = modifier(width=10)
    >>> controls = map(w10, controls)
    >>> class Object(object): pass
    >>> w10 = modifier(width=10)
    >>> cls = w10(Object())
    >>> cls.width
    10

    """
    return lambda obj: modify(obj, **kwargs)


def find_element_by_type(container, cls):
    """
    Поиск экземпляров элементов во всех вложенных контейнерах

    :param container: Контейнер
    :type container: m3_ext.ui.containers.containers.ExtContainer
    :param cls: Класс
    :type cls: types.ClassType
    """
    res = []
    for item in container.items:
        if isinstance(item, cls):
            res.append(item)

        if hasattr(item, 'items'):
            res.extend(find_element_by_type(item, cls))
    return res


#==============================================================================
# collect_overlaps
#==============================================================================
def collect_overlaps(obj, queryset, attr_begin='begin', attr_end='end'):
    """
    Возвращает список объектов из указанной выборки, которые пересекаются
    с указанным объектом по указанным полям начала и конца интервала

    :param obj: Объект
    :param queryset: Выборка
    :type queryset: django.db.models.query.QuerySet
    :param attr_begin: Атрибут модели с датой начала
    :type attr_begin: str
    :param attr_end: Атрибут модели с датой конца
    :type attr_end: str
    """
    obj_bgn = getattr(obj, attr_begin, None)
    obj_end = getattr(obj, attr_end, None)

    if obj_bgn is None or obj_end is None:
        raise ValueError(
            u'Объект интервальной модели должен иметь '
            u'непустые границы интервала!')

    if obj.id:
        queryset = queryset.exclude(id=obj.id)

    result = []
    for item in queryset.iterator():
        bgn = getattr(item, attr_begin, None)
        end = getattr(item, attr_end, None)
        if bgn is None or end is None:
            raise ValueError(
                u'Среди объектов выборки присутствуют некорректные!')

        def add():
            if any((
                bgn <= obj_bgn <= end,
                bgn <= obj_end <= end,
                obj_bgn <= bgn <= obj_end,
                obj_bgn <= end <= obj_end,
            )):
                result.append(item)

        try:
            add()
        except TypeError:
            if isinstance(obj_bgn, datetime.datetime) and isinstance(
                    obj_end, datetime.datetime):
                obj_bgn = obj_bgn.date()
                obj_end = obj_end.date()
                add()
    return result


#==============================================================================
# istraversable - проверка на "обходимость"
#==============================================================================
def istraversable(obj):
    """
    возвращает True, если объект :attr:`obj` позволяет обход себя в цикле `for`
    """
    return (
        hasattr(obj, '__iter__')
        or hasattr(obj, '__next__')
        or hasattr(obj, '__getitem__')
    )


#==============================================================================
# Кэширующий декоратор
#==============================================================================
def cached_to(attr_name):
    """
    Оборачивает простые методы (без аргументов) и property getters,
    с целью закэшировать первый полученный результат

    :param attr_name: Куда кэшировать
    :type attr_name: str
    """
    def wrapper(func):
        @wraps(func)
        def inner(self):
            if hasattr(self, attr_name):
                result = getattr(self, attr_name)
            else:
                result = func(self)
                setattr(self, attr_name, result)
            return result
        return inner
    return wrapper

#==============================================================================
def matcher(include=None, exclude=None):
    """
    Возвращет предикат, возвращающий True для строк,
    подходящих под образцы из include и
    не подходящих под образцы из exclude
    Образцы имеют вид
    - 'xyz'  -> строка должна полностью совпасть с образцом
    - '*xyz' -> строка должна оканчиваться образцом
    - 'xyz*' -> строка должна начинаться с образца

    >>> filter(matcher(['a*', 'b*', '*s', 'cat', 'dog'], ['*ee', 'dog']),
    ...        'cat ape apple duck see bee bean knee dog'.split())
    ['cat', 'ape', 'apple', 'bean']

    >>> filter(mather(include=['*s']), ['hour', 'hours', 'day', 'days'])
    ['hours', 'days']

    >>> filter(mather(exclude=['*s']), ['hour', 'hours', 'day', 'days'])
    ['hour', 'day']
    """
    def make_checker(patterns, default=True):
        matchers = []
        for pattern in list(patterns or ()):
            if pattern.endswith('*'):
                func = (lambda p: lambda s: s.startswith(p))(pattern[:-1])
            elif pattern.startswith('*'):
                func = (lambda p: lambda s: s.endswith(p))(pattern[1:])
            else:
                func = (lambda p: lambda s: s == p)(pattern)
            matchers.append(func)
        if matchers:
            return lambda s: any(func(s) for func in matchers)
        else:
            return lambda s: default

    must_be_keeped = make_checker(include)
    must_be_droped = make_checker(exclude, default=False)
    return lambda x: must_be_keeped(x) and not must_be_droped(x)

#==============================================================================
# парсеры для декларации контекста
#==============================================================================
def int_or_zero(raw_str):
    """
    >>> int_or_zero('')
    0
    >>> int_or_zero('10')
    10
    """
    return 0 if not raw_str else int(raw_str)

def int_or_none(raw_str):
    """
    >>> int_or_none('')
    None
    >>> int_or_none('10')
    10
    """
    return None if not raw_str else int(raw_str)

def int_list(raw_str):
    """
    >>> int_list('10,20, 30')
    [10, 20, 30]
    """
    return [int(i.strip()) for i in raw_str.split(',')]

#==============================================================================
# model to dict
#==============================================================================
def model_to_dict(obj, include=None, exclude=None):
    """
    Сериализует объект модели в словарь полностью или частично
    в зависимости от допусков и исключений

    Исключения и допуски имеют вид:
    - 'person'
    - '*_id'
    - 'user*'

    :type include: list
    :return: словарь - результат сериализации модели
    :param exclude: список исключений
    :type exclude: list
    :param include: список допусков
    :rtype: dict
    """
    permitted = matcher(include, exclude)
    res = {}
    for fld in obj.__class__._meta.fields:
        attr = fld.attname
        if permitted(attr):
            is_fk = isinstance(fld, _RelatedField)
            if is_fk:
                assert fld.attname.endswith('_id')
                attr = attr[:-3]
            try:
                val = _attrgetter(attr)(obj)
            except AttributeError:
                continue
            if is_fk:
                val = {
                    'id': getattr(val, 'id', None),
                    'name': unicode(val)
                }
            res[fld.attname] = val
    return res
