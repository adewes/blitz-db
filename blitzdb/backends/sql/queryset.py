import time
import copy
import sqlalchemy

from blitzdb.queryset import QuerySet as BaseQuerySet
from functools import wraps
from sqlalchemy.sql import select,func,expression,delete,distinct,and_,union,intersect
from sqlalchemy.sql.expression import join,asc,desc,outerjoin
from ..file.serializers import JsonSerializer
from .helpers import get_value
from collections import OrderedDict
from blitzdb.fields import ManyToManyField,ForeignKeyField,OneToManyField

class QuerySet(BaseQuerySet):

    def __init__(self, backend, table, cls,
                 condition = None,
                 select = None,
                 intersects = None,
                 raw = False,
                 include = None,
                 only = None,
                 joins = None,
                 extra_fields = None,
                 group_bys = None,
                 order_bys = None,
                 objects = None,
                 havings = None,
                 limit = None,
                 offset = None
                 ):
        super(QuerySet,self).__init__(backend = backend,cls = cls)

        self.joins = joins
        self.backend = backend
        self.condition = condition
        self.select = select
        self.havings = havings
        self.only = only
        self.include = include
        self.extra_fields = extra_fields
        self.group_bys = group_bys
        self.cls = cls
        self._limit = limit
        self._offset = offset
        self.table = table
        self.raw = raw
        self.intersects = intersects
        self.objects = objects
        if self.objects:
            self.pop_objects = self.objects[:]

        self.deserialized_objects = None
        self.deserialized_pop_objects = None
        self._it = None
        self.order_bys = order_bys
        self.count = None
        self.result = None

    def limit(self,limit):
        self._limit = limit
        return self

    def offset(self,offset):
        self._offset = offset
        return self

    def deserialize(self, data):

        d,lazy = self.backend.deserialize_db_data(data)

        if self.raw:
            return d

        obj = self.backend.create_instance(self.cls, d,lazy = lazy)
        obj.attributes = self.backend.deserialize(obj.lazy_attributes,create_instance = False)

        return obj

    def sort(self, keys,direction = None):
        #we sort by a single argument
        if direction:
            keys = ((keys,direction),)
        order_bys = []
        for key,direction in keys:
            if direction > 0:
                direction = asc
            else:
                direction = desc
            order_bys.append((key,direction))
        self.order_bys = order_bys
        self.objects = None
        return self

    def next(self):
        if self._it is None:
            self._it = iter(self)
        return self._it.next()

    __next__ = next

    def __iter__(self):
        if self.deserialized_objects is None:
            self.get_deserialized_objects()
        for obj in self.deserialized_objects:
            yield obj
        raise StopIteration

    def __contains__(self, obj):
        pks = self.distinct_pks()
        if isinstance(obj, list) or isinstance(obj, tuple):
            obj_list = obj
        else:
            obj_list = [obj]
        for obj in obj_list:
            if obj.pk not in pks:
                return False
        return True

    def get_deserialized_objects(self):
        if self.objects is None:
            self.get_objects()

        self.deserialized_objects = [self.deserialize(obj) for obj in self.objects]
        self.deserialized_pop_objects = self.deserialized_objects[:]

    def get_select_table_and_rows(self):

        columns = []
        column_map = {}
        joins = []

        def join_table(collection,table,key,params,path = None,key_path = None):
            if path is None:
                path = []
            if key_path is None:
                key_path = []
            if isinstance(params['relation']['field'],ManyToManyField):
                join_many_to_many(collection,table,key,params,path,key_path)
            elif isinstance(params['relation']['field'],ForeignKeyField):
                join_foreign_key(collection,table,key,params,path,key_path)
            elif isinstance(params['relation']['field'],OneToManyField):
                join_one_to_many(collection,table,key,params,path,key_path)
            else:
                raise AttributeError

        def process_fields_and_subkeys(related_collection,related_table,params,path,key_path):

            params['table_fields'] = {}
            for field,column_name in params['fields'].items():
                column_label = '_'.join(path+[column_name])
                params['table_fields'][field] = column_label
                column = related_table.c[column_name].label(column_label)
                columns.append(column)
                if field != '__data__':
                    column_map[".".join(key_path+[field])] = column

            for subkey,subparams in sorted(params['joins'].items(),key = lambda i : i[0]):
                join_table(params['collection'],related_table,subkey,subparams,path = path,key_path = key_path+[subkey])

        def join_one_to_many(collection,table,key,params,path,key_path):
            related_table = params['table'].alias()
            related_collection = params['relation']['collection']
            condition = table.c['pk'] == related_table.c[params['relation']['backref']['column']]
            joins.append((related_table,condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+\
                                        [params['relation']['backref']['column']],key_path)

        def join_foreign_key(collection,table,key,params,path,key_path):
            related_table = params['table'].alias()
            related_collection = params['relation']['collection']
            condition = table.c[params['relation']['column']] == related_table.c.pk
            joins.append((related_table,condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+\
                                        [params['relation']['column']],key_path)

        def join_many_to_many(collection,table,key,params,path,key_path):
            relationship_table = params['relation']['relationship_table'].alias()
            related_collection = params['relation']['collection']
            related_table = self.backend.get_collection_table(related_collection).alias()
            left_condition = relationship_table.c['pk_%s' % collection] == table.c.pk
            right_condition = relationship_table.c['pk_%s' % related_collection] == related_table.c.pk
            joins.append((relationship_table,left_condition))
            joins.append((related_table,right_condition))
            process_fields_and_subkeys(related_collection,related_table,params,path+[key],key_path)

        if self.include:
            include = copy.deepcopy(self.include)
            if isinstance(include,tuple):
                include = list(include)
            if not isinstance(include,list):
                raise AttributeError("include must be a list/tuple")
        else:
            include = []

        exclude = []
        if self.only:
            if isinstance(self.only,dict):
                only = []
                for key,value in self.only.items():
                    if value is False:
                        exclude.append(key)
                    else:
                        only.append(key)
            else:
                only = set(self.only)

            for only_key in only:
                if not only_key in include:
                    include.append(only_key)

        #problem: if we include a foreign object, we will trigger 
        order_by_keys = []
        if self.order_bys:
            for key,direction in self.order_bys:
                order_by_keys.append(key)

        self.include_joins = self.backend.get_include_joins(self.cls,
                                                            includes = include,
                                                            excludes = exclude,
                                                            order_by_keys = order_by_keys)

        #we only select the columns that we actually need
        my_columns = self.include_joins['fields'].values()+\
                     [params['relation']['column'] for params in self.include_joins['joins'].values()
                      if isinstance(params['relation']['field'],ForeignKeyField)]
        s = self.get_select(columns = [self.table.c[column] for column in my_columns],strict_order_by = False)
        s_cte = s.cte(name = 'results')

        process_fields_and_subkeys(self.include_joins['collection'],s_cte,self.include_joins,[],[])

        if joins:
            for i,j in enumerate(joins):
                s_cte = s_cte.outerjoin(*j)

        order_bys = []
        if self.order_bys:
            order_bys = [direction(column_map[key]) for (key,direction) in self.order_bys]

        return s_cte,order_bys,columns

    def as_table(self):
        return self.get_select_table_and_rows()[0]

    def get_objects(self):

        def build_field_map(params,path = None,current_map = None):

            def m2m_o2m_getter(join_params,name,pk_key):

                def f(d,obj):
                    pk_value = obj[pk_key]
                    try:
                        v = d[name]
                    except KeyError:
                        v = d[name] = OrderedDict()
                    if pk_value is None:
                        return None
                    if not pk_value in v:
                        v[pk_value] = {}
                    if not '__lazy__' in v[pk_value]:
                        v[pk_value]['__lazy__'] = join_params['lazy']
                    if not '__collection__' in v[pk_value]:
                        v[pk_value]['__collection__'] = join_params['collection']
                    return v[pk_value]

                return f

            def fk_getter(join_params,key):

                def f(d,obj):
                    pk_value = obj[join_params['table_fields']['pk']]
                    if pk_value is None:
                        return None
                    if not key in d:
                        d[key] = {}
                    v = d[key]
                    if not '__lazy__' in v:
                        v['__lazy__'] = join_params['lazy']
                    if not '__collection__' in v:
                        v['__collection__'] = join_params['collection']
                    return v

                return f

            if current_map is None:
                current_map = {}
            if path is None:
                path = []
            for key,field in params['table_fields'].items():
                if key in params['joins']:
                    continue
                current_map[field] = path+[key]
            for name,join_params in params['joins'].items():
                if name in current_map:
                    del current_map[name]
                if isinstance(join_params['relation']['field'],(ManyToManyField,OneToManyField)):
                    build_field_map(join_params,path+[m2m_o2m_getter(join_params,name,
                                                                 join_params['table_fields']['pk'])],current_map)
                else:
                    build_field_map(join_params,path+[fk_getter(join_params,name),],current_map)
            return current_map

        def replace_ordered_dicts(d):
            for key,value in d.items():
                if isinstance(value,OrderedDict):
                    replace_ordered_dicts(value)
                    d[key] = list(value.values())
                elif isinstance(value,dict):
                    d[key] = replace_ordered_dicts(value)
            return d

        s_cte,order_bys,rows = self.get_select_table_and_rows()

        field_map = build_field_map(self.include_joins)
        with self.backend.transaction(use_auto = False):
            try:
                result = self.backend.connection.execute(select(rows).select_from(s_cte).order_by(*order_bys))
                if result.returns_rows:
                    objects = list(result.fetchall())
                else:
                    objects = []
            except sqlalchemy.exc.ResourceClosedError:
                objects = None
                raise

        #we "fold" the objects back into one list structure
        self.objects = []
        pks = []

        unpacked_objects = OrderedDict()
        for obj in objects:
            if not obj['pk'] in unpacked_objects:
                unpacked_objects[obj['pk']] = {'__lazy__' : self.include_joins['lazy'],
                                               '__collection__' : self.include_joins['collection']}
            unpacked_obj = unpacked_objects[obj['pk']]
            for key,path in field_map.items():
                d = unpacked_obj
                for element in path[:-1]:
                    if callable(element):
                        d = element(d,obj)
                        if d is None:
                            break
                    else:
                        d = get_value(d,element,create = True)
                else:
                    d[path[-1]] = obj[key]

        self.objects = [replace_ordered_dicts(unpacked_obj) for unpacked_obj in unpacked_objects.values()]
        self.pop_objects = self.objects[:]

    def as_list(self):
        if self.deserialized_objects is None:
            self.get_deserialized_objects()
        return [obj for obj in self.deserialized_objects]

    def __getitem__(self,key):
        if isinstance(key, slice):
            start, stop, step = key.start, key.stop, key.step
            if step != None:
                raise IndexError("SQL backend dos not support steps in slices")
            if key.start == None:
                start = 0
            if key.stop == None:
                stop = len(self)
            if start < 0:
                start = len(self) + start
            if stop < 0:
                stop = len(self) + stop
            qs = copy.copy(self)
            if start:
                qs.offset(start)
            qs.limit(stop-start)
            qs.objects = None
            qs.count = None
            return qs
        if self.deserialized_objects is None:
            self.get_deserialized_objects()
        return self.deserialized_objects[key]

    def pop(self,i = 0):
        if self.deserialized_objects is None:
            self.get_deserialized_objects()
        if self.deserialized_pop_objects:
            return self.deserialized_pop_objects.pop()
        raise IndexError("pop from empty list")

    def filter(self,*args,**kwargs):
        qs = self.backend.filter(self.cls,*args,**kwargs)
        return self.intersect(qs)

    def intersect(self,qs):
        #here the .self_group() is necessary to ensure the correct grouping within the INTERSECT...
        my_select = self.get_select(columns = [self.table.c.pk]).self_group()
        qs_select = qs.get_select(columns = [qs.table.c.pk]).self_group()
        condition = and_(self.table.c.pk.in_(expression.intersect(my_select,qs_select)))
        new_qs = QuerySet(self.backend,
                          self.table,
                          self.cls,
                          condition = condition,
                          order_bys = self.order_bys,
                          raw = self.raw,
                          include = self.include,
                          only = self.only,
                          extra_fields = self.extra_fields)
        return new_qs

    def delete(self):
        with self.backend.transaction(use_auto = False):
            delete_stmt = self.table.delete().where(self.table.c.pk.in_(self.get_select(columns = [self.table.c.pk])))
            self.backend.connection.execute(delete_stmt)

    def get_fields(self):
        return [column for column in self.table.columns]

    def get_select(self,columns = None,order_by = True,strict_order_by = True):
        if self.select is not None:
            return self.select
        if columns is None:
            columns = self.get_fields()
        if self.extra_fields:
            columns.extend(self.extra_fields)

        s = select(columns)
        if self.joins:
            full_join = None
            for j in self.joins:
                if full_join is not None:
                    full_join = full_join.join(*j)
                else:
                    full_join = outerjoin(self.table,*j)
            s = s.select_from(full_join)

        if self.condition is not None:
            s = s.where(self.condition)

        if self.joins:
            if self.group_bys:
                my_group_bys = self.group_bys[:]
            else:
                my_group_bys = []
            for column in columns:
                if not column in my_group_bys:
                    my_group_bys.append(column)
        else:
            my_group_bys = self.group_bys
        if my_group_bys:
            s = s.group_by(*my_group_bys)
        if self.havings:
            for having in self.havings:
                s = s.having(having)
        if order_by and self.order_bys:
            order_by_list = []
            for key,direction in self.order_bys:
                try:
                    order_by_list.append(direction(self.table.c[self.backend.get_column_for_key(self.cls,key)]))
                except KeyError:
                    if strict_order_by:
                        raise
            s = s.order_by(*order_by_list)
        if self._offset:
            s = s.offset(self._offset)
        if self._limit:
            s = s.limit(self._limit)
        return s

    def __len__(self):
        if self.count is None:
            if self.objects is not None:
                self.count = len(self.objects)
            else:
                with self.backend.transaction(use_auto = False):
                    s = select([func.count()]).select_from(self.get_select(columns = [self.table.c.pk],strict_order_by = False).alias('count_select'))
                    result = self.backend.connection.execute(s)
                    self.count = result.first()[0]
                    result.close()
        return self.count

    def distinct_pks(self):
        with self.backend.transaction(use_auto = False):
            s = self.get_select([self.table.c.pk]).distinct(self.table.c.pk)
            result = self.backend.connection.execute(s)
            return set([r[0] for r in result.fetchall()])
        
    def __ne__(self, other):
        return not self.__eq__(other)
    
    def __eq__(self, other):
        if isinstance(other, QuerySet): 
            if self.cls == other.cls and len(self) == len(other) \
              and self.distinct_pks() == other.distinct_pks():
                return True
        elif isinstance(other, list):
            if len(other) != len(self.keys):
                return False
            objs = list(self)
            if other == objs:
                return True
        return False

