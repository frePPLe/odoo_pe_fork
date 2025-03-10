# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 by frePPLe bv
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

import json
import logging
import pytz
import xmlrpc.client
from xml.sax.saxutils import quoteattr
from datetime import datetime, timedelta
from pytz import timezone
import ssl

with_mrp = True

try:
    import odoo
except ImportError:
    pass

logger = logging.getLogger(__name__)


class Odoo_generator:
    def __init__(self, env):
        self.env = env

    def setContext(self, **kwargs):
        t = dict(self.env.context)
        t.update(kwargs)
        self.env = self.env(
            user=self.env.user,
            context=t,
        )

    def callMethod(self, model, id, method, args=[]):
        for obj in self.env[model].browse(id):
            return getattr(obj, method)(*args)
        return None

    def getData(self, model, search=[], order=None, fields=[], ids=None):
        if ids is not None:
            return self.env[model].browse(ids).read(fields) if ids else []
        if order:
            return self.env[model].search(search, order=order).read(fields)
        else:
            return self.env[model].search(search).read(fields)


class XMLRPC_generator:
    pagesize = 5000

    def __init__(self, url, db, username, password):
        self.db = db
        self.password = password
        self.env = xmlrpc.client.ServerProxy(
            "{}/xmlrpc/2/common".format(url),
            context=ssl._create_unverified_context(),
        )
        self.uid = self.env.authenticate(db, username, password, {})
        self.env = xmlrpc.client.ServerProxy(
            "{}/xmlrpc/2/object".format(url),
            context=ssl._create_unverified_context(),
            use_builtin_types=True,
            headers={"Connection": "keep-alive"}.items(),
        )
        self.context = {}

    def setContext(self, **kwargs):
        self.context.update(kwargs)

    def callMethod(self, model, id, method, args):
        return self.env.execute_kw(
            self.db, self.uid, self.password, model, method, [id], []
        )

    def getData(self, model, search=None, order="id asc", fields=[], ids=[]):
        if ids:
            page_ids = [ids]
        else:
            page_ids = []
            offset = 0
            msg = {
                "limit": self.pagesize,
                "offset": offset,
                "context": self.context,
                "order": order,
            }
            while True:
                extra_ids = self.env.execute_kw(
                    self.db,
                    self.uid,
                    self.password,
                    model,
                    "search",
                    [search] if search else [[]],
                    msg,
                )
                if not extra_ids:
                    break
                page_ids.append(extra_ids)
                offset += self.pagesize
                msg["offset"] = offset
        if page_ids and page_ids != [[]]:
            data = []
            for page in page_ids:
                data.extend(
                    self.env.execute_kw(
                        self.db,
                        self.uid,
                        self.password,
                        model,
                        "read",
                        [page],
                        {"fields": fields, "context": self.context},
                    )
                )
            return data
        else:
            return []


class exporter(object):
    def __init__(
        self,
        generator,
        req,
        uid,
        database=None,
        company=None,
        mode=1,
        timezone=None,
        singlecompany=False,
        version="0.0.0.unknown",
    ):
        self.database = database
        self.company = company
        self.generator = generator
        self.version = version
        self.timezone = timezone
        if timezone:
            if timezone not in pytz.all_timezones:
                logger.warning("Invalid timezone URL argument: %s." % (timezone,))
                self.timezone = None
            else:
                # Valid timezone override in the url
                self.timezone = timezone
        if not self.timezone:
            # Default timezone: use the timezone of the connector user (or UTC if not set)
            for i in self.generator.getData(
                "res.users",
                ids=[uid],
                fields=["tz"],
            ):
                self.timezone = i["tz"] or "UTC"
        self.timeformat = "%Y-%m-%dT%H:%M:%S"
        self.singlecompany = singlecompany

        # The mode argument defines different types of runs:
        #  - Mode 1:
        #    This mode returns all data that is loaded with every planning run.
        #    Currently this mode transfers all objects, except closed sales orders.
        #  - Mode 2:
        #    This mode returns data that is loaded that changes infrequently and
        #    can be transferred during automated scheduled runs at a quiet moment.
        #    Currently this mode transfers only closed sales orders.
        #
        # Normally an Odoo object should be exported by only a single mode.
        # Exporting a certain object with BOTH modes 1 and 2 will only create extra
        # processing time for the connector without adding any benefits. On the other
        # hand it won't break things either.
        #
        # Which data elements belong to each mode can vary between implementations.
        self.mode = mode

    def run(self):
        # Check if we manage by work orders or manufacturing orders.
        self.manage_work_orders = False
        for rec in self.generator.getData(
            "ir.model", search=[("model", "=", "mrp.workorder")], fields=["name"]
        ):
            self.manage_work_orders = True

        # Load some auxiliary data in memory
        self.load_company()
        if self.mode == 0:
            # This was only a connection test
            yield '<?xml version="1.0" encoding="UTF-8" ?>\n'
            yield '<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" source="odoo_%s">' % self.mode
            yield "connection ok"
            yield "</plan>"
            return

        self.load_uom()

        # Header.
        # The source attribute is set to 'odoo_<mode>', such that all objects created or
        # updated from the data are also marked as from originating from odoo.
        yield '<?xml version="1.0" encoding="UTF-8" ?>\n'
        yield '<plan xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" source="odoo_%s">\n' % self.mode
        yield "<description>Generated by odoo %s</description>\n" % odoo.release.version

        # Synchronize users
        yield from self.export_users()

        # Main content.
        # The order of the entities is important. First one needs to create the
        # objects before they are referenced by other objects.
        # If multiple types of an entity exists (eg operation_time_per,
        # operation_alternate, operation_alternate, etc) the reference would
        # automatically create an object, potentially of the wrong type.
        if with_mrp:
            logger.debug("Exporting calendars.")
            if self.mode == 1:
                yield from self.export_calendar()
        logger.debug("Exporting locations.")
        yield from self.export_locations()
        logger.debug("Exporting customers.")
        yield from self.export_customers()
        if self.mode == 1:
            logger.debug("Exporting suppliers.")
            yield from self.export_suppliers()
            if with_mrp:
                logger.debug("Exporting skills.")
                yield from self.export_skills()
                logger.debug("Exporting workcenters.")
                yield from self.export_workcenters()
                logger.debug("Exporting workcenterskills.")
                yield from self.export_workcenterskills()
        logger.debug("Exporting products.")
        yield from self.export_items()
        if with_mrp:
            logger.debug("Exporting BOMs.")
            if self.mode == 1:
                yield from self.export_boms()
        logger.debug("Exporting sales orders.")
        yield from self.export_salesorders()
        # Uncomment the following lines to create forecast models in frepple
        # logger.debug("Exporting forecast.")
        # for i in self.export_forecasts():
        #     yield i
        if self.mode == 1:
            logger.debug("Exporting purchase orders.")
            yield from self.export_purchaseorders()
            if with_mrp:
                logger.debug("Exporting manufacturing orders.")
                yield from self.export_manufacturingorders()
            logger.debug("Exporting reordering rules.")
            yield from self.export_orderpoints()
            logger.debug("Exporting quantities on-hand.")
            yield from self.export_onhand()

        # Footer
        yield "</plan>\n"

    def load_company(self):
        self.company_id = 0
        for i in self.generator.getData(
            "res.company",
            search=[("name", "=", self.company)],
            fields=[
                "security_lead",
                "po_lead",
                "manufacturing_lead",
                "calendar",
                "manufacturing_warehouse",
                "respect_reservations",
            ],
        ):
            self.company_id = i["id"]
            self.security_lead = int(
                i["security_lead"]
            )  # TODO NOT USED RIGHT NOW - add parameter in frepple for this
            self.po_lead = i["po_lead"]
            self.manufacturing_lead = i["manufacturing_lead"]
            self.respect_reservations = i["respect_reservations"]
            try:
                self.calendar = i["calendar"] and i["calendar"][1] or None
                self.mfg_location = (
                    i["manufacturing_warehouse"]
                    and i["manufacturing_warehouse"][1]
                    or self.company
                )
            except Exception:
                self.calendar = None
                self.mfg_location = None
            if self.singlecompany:
                # Create a new context to limit the data to the selected company
                self.generator.setContext(allowed_company_ids=[i["id"]])
        if not self.company_id:
            logger.warning("Can't find company '%s'" % self.company)
            self.company_id = None
            self.security_lead = 0
            self.po_lead = 0
            self.manufacturing_lead = 0
            self.calendar = None
            self.mfg_location = self.company

    def load_uom(self):
        """
        Loading units of measures into a dictionary for fast lookups.

        All quantities are sent to frePPLe as numbers, expressed in the default
        unit of measure of the uom dimension.
        """
        self.uom = {}
        self.uom_categories = {}
        for i in self.generator.getData(
            "uom.uom",
            # We also need to load INactive UOMs, because there still might be records
            # using the inactive UOM. Questionable practice, but can happen...
            search=["|", ("active", "=", 1), ("active", "=", 0)],
            fields=["factor", "uom_type", "category_id", "name"],
        ):
            if i["uom_type"] == "reference":
                self.uom_categories[i["category_id"][0]] = i["id"]
            self.uom[i["id"]] = {
                "factor": i["factor"],
                "category": i["category_id"][0],
                "name": i["name"],
            }

    def convert_qty_uom(self, qty, uom_id, product_template_id=None):
        """
        Convert a quantity to the reference uom of the product template.
        """
        try:
            uom_id = uom_id[0]
        except Exception as e:
            pass
        if not uom_id:
            return qty
        if not product_template_id:
            return qty * self.uom[uom_id]["factor"]
        try:
            product_uom = self.product_templates[product_template_id]["uom_id"][0]
        except Exception:
            return qty * self.uom[uom_id]["factor"]
        # check if default product uom is the one we received
        if product_uom == uom_id:
            return qty
        # check if different uoms belong to the same category
        if self.uom[product_uom]["category"] == self.uom[uom_id]["category"]:
            return qty / self.uom[uom_id]["factor"] * self.uom[product_uom]["factor"]
        else:
            # UOM is from a different category as the reference uom of the product.
            logger.warning(
                "Can't convert from %s for product template %s"
                % (self.uom[uom_id]["name"], product_template_id)
            )
            return qty * self.uom[uom_id]["factor"]

    def convert_float_time(self, float_time, units="days"):
        """
        Convert Odoo float time to ISO 8601 duration.
        """
        d = timedelta(**{units: float_time})
        return "P%dDT%dH%dM%dS" % (
            d.days,  # duration: days
            int(d.seconds / 3600),  # duration: hours
            int((d.seconds % 3600) / 60),  # duration: minutes
            int(d.seconds % 60),  # duration: seconds
        )

    def formatDateTime(self, d, tmzone=None):
        if not isinstance(d, datetime):
            d = datetime.fromisoformat(d)
        return d.astimezone(timezone(tmzone or self.timezone)).strftime(self.timeformat)

    def export_users(self):
        users = []
        for grp in self.generator.getData(
            "res.groups",
            search=[("name", "=", "frePPLe user")],
            fields=[
                "users",
            ],
        ):
            for usr in self.generator.getData(
                "res.users",
                ids=grp["users"],
                fields=["name", "login"],
            ):
                users.append(
                    (
                        usr["name"],
                        usr["login"],
                    )
                )
        yield '<stringproperty name="users" value=%s/>\n' % quoteattr(json.dumps(users))

    def export_calendar(self):
        """
        Reads all calendars from resource.calendar model and creates a calendar in frePPLe.
        Attendance times are read from resource.calendar.attendance
        Leave times are read from resource.calendar.leaves

        resource.calendar.name -> calendar.name (default value is 0)
        resource.calendar.attendance.date_from -> calendar bucket start date (or 2020-01-01 if unspecified)
        resource.calendar.attendance.date_to -> calendar bucket end date (or 2030-01-01 if unspecified)
        resource.calendar.attendance.hour_from -> calendar bucket start time
        resource.calendar.attendance.hour_to -> calendar bucket end time
        resource.calendar.attendance.dayofweek -> calendar bucket day

        resource.calendar.leaves.date_from -> calendar bucket start date
        resource.calendar.leaves.date_to -> calendar bucket end date

        For two-week calendars all weeks between the calendar start and
        calendar end dates are added in frepple as calendar buckets.
        The week number is using the iso standard (first week of the
        year is the one containing the first Thursday of the year).

        """
        yield "<!-- calendar -->\n"
        yield "<calendars>\n"

        calendars = {}
        cal_tz = {}
        cal_ids = set()
        try:
            # Read the timezone
            for i in self.generator.getData(
                "resource.calendar",
                fields=[
                    "name",
                    "tz",
                ],
            ):
                cal_tz[i["name"]] = i["tz"]
                cal_ids.add(i["id"])

            # Read the attendance for all calendars
            for i in self.generator.getData(
                "resource.calendar.attendance",
                search=[("display_type", "=", False)],
                fields=[
                    "dayofweek",
                    "date_from",
                    "date_to",
                    "hour_from",
                    "hour_to",
                    "calendar_id",
                    "week_type",
                ],
            ):
                if i["calendar_id"] and i["calendar_id"][0] in cal_ids:
                    if i["calendar_id"][1] not in calendars:
                        calendars[i["calendar_id"][1]] = []
                    i["attendance"] = True
                    calendars[i["calendar_id"][1]].append(i)

            # Read the leaves for all calendars
            for i in self.generator.getData(
                "resource.calendar.leaves",
                search=[("time_type", "=", "leave")],
                fields=[
                    "date_from",
                    "date_to",
                    "calendar_id",
                ],
            ):
                if i["calendar_id"] and i["calendar_id"][0] in cal_ids:
                    if i["calendar_id"][1] not in calendars:
                        calendars[i["calendar_id"][1]] = []
                    i["attendance"] = False
                    calendars[i["calendar_id"][1]].append(i)

            # Iterate over the results:
            for i in calendars:
                priority_attendance = 1000
                priority_leave = 10
                if cal_tz[i] != self.timezone:
                    logger.warning(
                        "timezone is different on workcenter %s and connector user. Working hours will not be synced correctly to frepple."
                        % i
                    )
                yield '<calendar name=%s default="0"><buckets>\n' % quoteattr(i)
                for j in calendars[i]:
                    if j.get("week_type", False) == False:
                        # ONE-WEEK CALENDAR
                        yield '<bucket start="%s" end="%s" value="%s" days="%s" priority="%s" starttime="%s" endtime="%s"/>\n' % (
                            self.formatDateTime(j["date_from"], cal_tz[i])
                            if not j["attendance"]
                            else (
                                j["date_from"].strftime("%Y-%m-%dT00:00:00")
                                if j["date_from"]
                                else "2020-01-01T00:00:00"
                            ),
                            self.formatDateTime(j["date_to"], cal_tz[i])
                            if not j["attendance"]
                            else (
                                j["date_to"].strftime("%Y-%m-%dT00:00:00")
                                if j["date_to"]
                                else "2030-01-01T00:00:00"
                            ),
                            "1" if j["attendance"] else "0",
                            (2 ** ((int(j["dayofweek"]) + 1) % 7))
                            if "dayofweek" in j
                            else (2**7) - 1,
                            priority_attendance if j["attendance"] else priority_leave,
                            # In odoo, monday = 0. In frePPLe, sunday = 0.
                            ("PT%dM" % round(j["hour_from"] * 60))
                            if "hour_from" in j
                            else "PT0M",
                            ("PT%dM" % round(j["hour_to"] * 60))
                            if "hour_to" in j
                            else "PT1440M",
                        )
                        if j["attendance"]:
                            priority_attendance += 1
                        else:
                            priority_leave += 1
                    else:
                        # TWO-WEEKS CALENDAR
                        start = j["date_from"] or datetime(2020, 1, 1)
                        end = j["date_to"] or datetime(2030, 1, 1)

                        t = start
                        while t < end:
                            if int(t.isocalendar()[1] % 2) == int(j["week_type"]):
                                if j["hour_to"] == 0:
                                    logger.info(j)
                                yield '<bucket start="%s" end="%s" value="%s" days="%s" priority="%s" starttime="%s" endtime="%s"/>\n' % (
                                    self.formatDateTime(t, cal_tz[i]),
                                    self.formatDateTime(
                                        min(t + timedelta(7 - t.weekday()), end),
                                        cal_tz[i],
                                    ),
                                    "1",
                                    (2 ** ((int(j["dayofweek"]) + 1) % 7))
                                    if "dayofweek" in j
                                    else (2**7) - 1,
                                    priority_attendance,
                                    # In odoo, monday = 0. In frePPLe, sunday = 0.
                                    ("PT%dM" % round(j["hour_from"] * 60))
                                    if "hour_from" in j
                                    else "PT0M",
                                    ("PT%dM" % round(j["hour_to"] * 60))
                                    if "hour_to" in j
                                    else "PT1440M",
                                )
                                priority_attendance += 1
                            dow = t.weekday()
                            t += timedelta(7 - dow)

                yield "</buckets></calendar>\n"

            yield "</calendars>\n"
        except Exception as e:
            logger.info(e)
            yield "</calendars>\n"

    def export_locations(self):
        """
        Generate a list of warehouse locations to frePPLe, based on the
        stock.warehouse model.

        We assume the location name to be unique. This is NOT guaranteed by Odoo.

        The field subcategory is used to store the id of the warehouse. This makes
        it easier for frePPLe to send back planning results directly with an
        odoo location identifier.

        FrePPLe is not interested in the locations odoo defines with a warehouse.
        This methods also populates a map dictionary between these locations and
        warehouse they belong to.

        Mapping:
        stock.warehouse.name -> location.name
        stock.warehouse.id -> location.subcategory
        """
        self.map_locations = {}
        self.warehouses = {}
        first = True
        for i in self.generator.getData(
            "stock.warehouse",
            fields=["name"],
        ):
            if first:
                yield "<!-- warehouses -->\n"
                yield "<locations>\n"
                first = False
            if self.calendar:
                yield '<location name=%s subcategory="%s"><available name=%s/></location>\n' % (
                    quoteattr(i["name"]),
                    i["id"],
                    quoteattr(self.calendar),
                )
            else:
                yield '<location name=%s subcategory="%s"></location>\n' % (
                    quoteattr(i["name"]),
                    i["id"],
                )
            self.warehouses[i["id"]] = i["name"]
        if not first:
            yield "</locations>\n"

        # Populate a mapping location-to-warehouse name for later lookups
        loc_ids = [
            loc["id"]
            for loc in self.generator.getData(
                "stock.location",
                search=[("usage", "=", "internal")],
                fields=["id"],
            )
        ]

        for loc_object in self.generator.getData(
            "stock.location",
            ids=loc_ids,
        ):
            if (
                loc_object.get("warehouse_id", False)
                and loc_object["warehouse_id"][0] in self.warehouses
            ):
                self.map_locations[loc_object["id"]] = self.warehouses[
                    loc_object["warehouse_id"][0]
                ]

    def export_customers(self):
        """
        Generate a list of customers to frePPLe, based on the res.partner model.
        We filter on res.partner where customer = True.

        Mapping:
        res.partner.id res.partner.name -> customer.name
        """
        self.map_customers = {}
        first = True
        for i in self.generator.getData(
            "res.partner",
            search=[("is_company", "=", True)],
            fields=["name"],
        ):
            if first:
                yield "<!-- customers -->\n"
                yield "<customers>\n"
                first = False
            name = "%s %s" % (i["name"], i["id"])
            yield "<customer name=%s/>\n" % quoteattr(name)
            self.map_customers[i["id"]] = name
        if not first:
            yield "</customers>\n"

    def export_suppliers(self):
        """
        Generate a list of suppliers for frePPLe, based on the res.partner model.
        We filter on res.supplier where supplier = True.

        Mapping:
        res.partner.id res.partner.name -> supplier.name
        """
        first = True
        for i in self.generator.getData(
            "res.partner",
            search=[("is_company", "=", True)],
            fields=["name"],
        ):
            if first:
                yield "<!-- suppliers -->\n"
                yield "<suppliers>\n"
                first = False
            yield "<supplier name=%s/>\n" % quoteattr("%d %s" % (i["id"], i["name"]))
        if not first:
            yield "</suppliers>\n"

    def export_skills(self):
        first = True
        for i in self.generator.getData(
            "mrp.skill",
            fields=["name"],
        ):
            if first:
                yield "<!-- skills -->\n"
                yield "<skills>\n"
                first = False
            name = i["name"]
            yield "<skill name=%s/>\n" % (quoteattr(name),)
        if not first:
            yield "</skills>\n"

    def export_workcenterskills(self):
        first = True
        for i in self.generator.getData(
            "mrp.workcenter.skill",
            fields=["workcenter", "skill", "priority"],
        ):
            if not i["workcenter"] or i["workcenter"][0] not in self.map_workcenters:
                continue
            if first:
                yield "<!-- resourceskills -->\n"
                yield "<skills>\n"
                first = False
            yield "<skill name=%s>\n" % quoteattr(i["skill"][1])
            yield "<resourceskills>"
            yield '<resourceskill priority="%d"><resource name=%s/></resourceskill>' % (
                i["priority"],
                quoteattr(self.map_workcenters[i["workcenter"][0]]),
            )
            yield "</resourceskills>"
            yield "</skill>"
        if not first:
            yield "</skills>"

    def export_workcenters(self):
        """
        Send the workcenter list to frePPLe, based one the mrp.workcenter model.

        We assume the workcenter name is unique. Odoo does NOT guarantuee that.

        Mapping:
        mrp.workcenter.name -> resource.name
        mrp.workcenter.owner -> resource.owner
        mrp.workcenter.resource_calendar_id -> resource.available
        mrp.workcenter.capacity -> resource.maximum
        mrp.workcenter.time_efficiency -> resource.efficiency

        company.mfg_location -> resource.location
        """
        self.map_workcenters = {}
        first = True
        for i in self.generator.getData(
            "mrp.workcenter",
            fields=[
                "name",
                "owner",
                "resource_calendar_id",
                "time_efficiency",
                "capacity",
                "tool",
            ],
        ):
            if first:
                yield "<!-- workcenters -->\n"
                yield "<resources>\n"
                first = False
            name = i["name"]
            owner = i["owner"]
            available = i["resource_calendar_id"]
            self.map_workcenters[i["id"]] = name
            yield '<resource name=%s maximum="%s" category="%s" subcategory="%s" efficiency="%s"><location name=%s/>%s%s</resource>\n' % (
                quoteattr(name),
                i["capacity"],
                i["id"],
                # Use this line if the tool use is independent of the MO quantity
                # "tool" if i["tool"] else "",
                # Use this line if the tool usage is proportional to the MO quantity
                "tool per piece" if i["tool"] else "",
                i["time_efficiency"],
                quoteattr(self.mfg_location),
                ("<owner name=%s/>" % quoteattr(owner[1])) if owner else "",
                ("<available name=%s/>" % quoteattr(available[1])) if available else "",
            )
        if not first:
            yield "</resources>\n"

    def export_items(self):
        """
        Send the list of products to frePPLe, based on the product.product model.
        For purchased items we also create a procurement buffer in each warehouse.

        Mapping:
        [product.product.code] product.product.name -> item.name
        product.product.product_tmpl_id.list_price or standard_price -> item.cost
        product.product.id , product.product.product_tmpl_id.uom_id -> item.subcategory

        If product.product.product_tmpl_id.purchase_ok
        we collect the suppliers as product.product.product_tmpl_id.seller_ids
        [product.product.code] product.product.name -> itemsupplier.item
        res.partner.id res.partner.name -> itemsupplier.supplier.name
        supplierinfo.delay -> itemsupplier.leadtime
        supplierinfo.min_qty -> itemsupplier.size_minimum
        supplierinfo.date_start -> itemsupplier.effective_start
        supplierinfo.date_end -> itemsupplier.effective_end
        product.product.product_tmpl_id.delay -> itemsupplier.leadtime
        supplierinfo.sequence -> itemsupplier.priority
        """

        # Read the product templates
        self.product_product = {}
        self.product_template_product = {}
        self.product_templates = {}
        for i in self.generator.getData(
            "product.template",
            search=[("type", "not in", ("service", "consu"))],
            fields=[
                "sale_ok",
                "purchase_ok",
                "produce_delay",
                "list_price",
                "standard_price",
                "uom_id",
                "categ_id",
                "product_variant_ids",
                "expiration_time",
            ],
        ):
            self.product_templates[i["id"]] = i

        # Read the products
        supplierinfo_fields = [
            "name",
            "delay",
            "min_qty",
            "date_end",
            "date_start",
            "price",
            "batching_window",
            "sequence",
            "is_subcontractor",
        ]
        first = True
        for i in self.generator.getData(
            "product.product",
            fields=[
                "id",
                "name",
                "code",
                "product_tmpl_id",
                "volume",
                "weight",
                "product_template_attribute_value_ids",
                "price_extra",
            ],
        ):
            if first:
                yield "<!-- products -->\n"
                yield "<items>\n"
                first = False
            if i["product_tmpl_id"][0] not in self.product_templates:
                continue
            tmpl = self.product_templates[i["product_tmpl_id"][0]]
            if i["code"]:
                name = ("[%s] %s" % (i["code"], i["name"]))[:300]
            # product is a variant and has no internal reference
            # we use the product id as code
            elif i["product_template_attribute_value_ids"]:
                name = ("[%s] %s" % (i["id"], i["name"]))[:300]
            else:
                name = i["name"][:300]
            prod_obj = {
                "name": name,
                "template": i["product_tmpl_id"][0],
                "product_template_attribute_value_ids": i[
                    "product_template_attribute_value_ids"
                ],
            }
            self.product_product[i["id"]] = prod_obj
            self.product_template_product[i["product_tmpl_id"][0]] = prod_obj
            # For make-to-order items the next line needs to XML snippet ' type="item_mto"'.
            yield '<item name=%s uom=%s volume="%f" weight="%f" cost="%f" category=%s subcategory="%s,%s"%s>\n' % (
                quoteattr(name),
                quoteattr(tmpl["uom_id"][1]) if tmpl["uom_id"] else "",
                i["volume"] or 0,
                i["weight"] or 0,
                max(
                    0, (tmpl["list_price"] + (i["price_extra"] or 0)) or 0
                )  # Option 1:  Map "sales price" to frepple
                #  max(0, tmpl["standard_price"]) or 0)  # Option 2: Map the "cost" to frepple
                / self.convert_qty_uom(1.0, tmpl["uom_id"], i["product_tmpl_id"][0]),
                quoteattr(tmpl["categ_id"][1]) if tmpl["categ_id"] else '""',
                self.uom_categories[self.uom[tmpl["uom_id"][0]]["category"]],
                i["id"],
                (' shelflife="%s"' % self.convert_float_time(tmpl["expiration_time"]))
                if tmpl["expiration_time"] and tmpl["expiration_time"] > 0
                else "",
            )
            # Export suppliers for the item, if the item is allowed to be purchased
            if tmpl["purchase_ok"]:
                try:
                    # TODO it's inefficient to run a query per product template.
                    results = self.generator.getData(
                        "product.supplierinfo",
                        search=[("product_tmpl_id", "=", tmpl["id"])],
                        fields=supplierinfo_fields,
                    )
                except Exception:
                    # subcontracting module not installed
                    supplierinfo_fields.remove("is_subcontractor")
                    results = self.generator.getData(
                        "product.supplierinfo",
                        search=[("product_tmpl_id", "=", tmpl["id"])],
                        fields=supplierinfo_fields,
                    )
                suppliers = {}
                for sup in results:
                    name = "%d %s" % (sup["name"][0], sup["name"][1])
                    if sup.get("is_subcontractor", False):
                        if not hasattr(tmpl, "subcontractors"):
                            tmpl["subcontractors"] = []
                        tmpl["subcontractors"].append(
                            {
                                "name": name,
                                "delay": sup["delay"],
                                "priority": sup["sequence"] or 1,
                                "size_minimum": sup["min_qty"],
                            }
                        )
                    elif (name, sup["date_start"]) in suppliers:
                        # If there are multiple records with the same supplier & start date
                        # we pass a single record to frepple with lowest-lead-time,
                        # lowest-quantity, lowest-sequence, greatest-end-date.
                        r = suppliers[(name, sup["date_start"])]
                        if sup["delay"] and (
                            not r["delay"] or sup["delay"] < r["delay"]
                        ):
                            r["delay"] = sup["delay"]
                        if sup["sequence"] and (
                            not r["sequence"] or sup["sequence"] < r["sequence"]
                        ):
                            r["sequence"] = sup["sequence"]
                        if sup["batching_window"] and (
                            not r["batching_window"]
                            or sup["batching_window"] > r["batching_window"]
                        ):
                            r["batching_window"] = sup["batching_window"]
                        if sup["min_qty"] and (
                            not r["min_qty"] or sup["min_qty"] < r["min_qty"]
                        ):
                            r["min_qty"] = sup["min_qty"]
                        if sup["price"] and (
                            not r["price"] or sup["price"] < r["price"]
                        ):
                            r["price"] = sup["price"]
                        if sup["date_end"] and (
                            not r["date_end"] or sup["date_end"] > r["date_end"]
                        ):
                            r["date_end"] = sup["date_end"]
                    else:
                        suppliers[(name, sup["date_start"])] = {
                            "delay": sup["delay"],
                            "sequence": sup["sequence"] or 1,
                            "batching_window": sup["batching_window"] or 0,
                            "min_qty": sup["min_qty"],
                            "price": max(0, sup["price"]),
                            "date_end": sup["date_end"],
                        }
                if suppliers:
                    yield "<itemsuppliers>\n"
                    for k, v in suppliers.items():
                        yield '<itemsupplier leadtime="P%dD" priority="%s" batchwindow="P%dD" size_minimum="%f" cost="%f"%s%s><supplier name=%s/></itemsupplier>\n' % (
                            v["delay"],
                            v["sequence"] or 1,
                            v["batching_window"] or 0,
                            v["min_qty"],
                            max(0, v["price"]),
                            ' effective_end="%sT00:00:00"'
                            % v["date_end"].strftime("%Y-%m-%d")
                            if v["date_end"]
                            else "",
                            ' effective_start="%sT00:00:00"' % k[1].strftime("%Y-%m-%d")
                            if k[1]
                            else "",
                            quoteattr(k[0]),
                        )
                    yield "</itemsuppliers>\n"
            yield "</item>\n"
        if not first:
            yield "</items>\n"

    def export_boms(self):
        """
        Exports mrp.routings, mrp.routing.workcenter and mrp.bom records into
        frePPLe operations, flows and loads.

        Not supported yet: a) parent boms, b) phantom boms.
        """
        yield "<!-- bills of material -->\n"
        yield "<operations>\n"

        # Read all active manufacturing routings
        # mrp_routings = {}
        # m = self.env["mrp.routing"]
        # recs = m.search([])
        # fields = ["location_id"]
        # for i in recs.read(fields):
        #    mrp_routings[i["id"]] = i["location_id"]

        # Read all workcenters of all routings
        mrp_routing_workcenters = {}
        for i in self.generator.getData(
            "mrp.routing.workcenter",
            order="bom_id, sequence, id asc",
            fields=[
                "name",
                "bom_id",
                "workcenter_id",
                "sequence",
                "time_cycle",
                "skill",
                "search_mode",
                "secondary_workcenter",
            ],
        ):
            if not i["bom_id"]:
                continue

            if i["bom_id"][0] in mrp_routing_workcenters:
                # If the same workcenter is used multiple times in a routing,
                # we add the times together.
                exists = False
                if not self.manage_work_orders:
                    for r in mrp_routing_workcenters[i["bom_id"][0]]:
                        if r["workcenter_id"][1] == i["workcenter_id"][1]:
                            r["time_cycle"] += i["time_cycle"]
                            exists = True
                            break
                if not exists:
                    mrp_routing_workcenters[i["bom_id"][0]].append(i)
            else:
                mrp_routing_workcenters[i["bom_id"][0]] = [i]

        # Loop over all secondary workcenters
        mrp_secondary_workcenter = {
            i["id"]: i for i in self.generator.getData("mrp.secondary.workcenter")
        }

        # Loop over all bom records
        for i in self.generator.getData(
            "mrp.bom",
            fields=[
                "product_qty",
                "product_uom_id",
                "product_tmpl_id",
                "product_id",
                "type",
                "bom_line_ids",
                "sequence",
            ],
        ):
            # Determine the location
            location = self.mfg_location

            product_template = self.product_templates.get(i["product_tmpl_id"][0], None)
            if not product_template:
                continue
            uom_factor = self.convert_qty_uom(
                1.0, i["product_uom_id"], i["product_tmpl_id"][0]
            )

            # Loop over all subcontractors
            if i["type"] == "subcontract":
                subcontractors = self.product_templates[i["product_tmpl_id"][0]].get(
                    "subcontractors", None
                )
                if not subcontractors:
                    continue
            else:
                subcontractors = [{}]

            for product_id in product_template["product_variant_ids"]:
                # In the case of variants, the BOM needs to apply to the correct product
                if i["product_id"] and not (i["product_id"][0] == product_id):
                    continue

                # Determine operation name and item
                product_buf = self.product_product.get(product_id, None)
                if not product_buf:
                    logger.warning("Skipping %s" % i["product_tmpl_id"][0])
                    continue

                for subcontractor in subcontractors:
                    # Build operation. The operation can either be a summary operation or a detailed
                    # routing.
                    operation = "%s @ %s %d" % (
                        product_buf["name"],
                        subcontractor.get("name", location),
                        i["id"],
                    )
                    if len(operation) > 300:
                        suffix = " @ %s %d" % (
                            subcontractor.get("name", location),
                            i["id"],
                        )
                        operation = "%s%s" % (
                            product_buf["name"][: 300 - len(suffix)],
                            suffix,
                        )
                    if (
                        not self.manage_work_orders
                        or subcontractor
                        or not mrp_routing_workcenters.get(i["id"], [])
                    ):
                        #
                        # CASE 1: A single operation used for the BOM
                        # All routing steps are collapsed in a single operation.
                        #
                        if subcontractor:
                            yield '<operation name=%s size_multiple="1" category="subcontractor" subcategory=%s duration="P%dD" posttime="P%dD" xsi:type="operation_fixed_time" priority="%s" size_minimum="%s">\n' "<item name=%s/><location name=%s/>\n" % (
                                quoteattr(operation),
                                quoteattr(subcontractor["name"]),
                                subcontractor.get("delay", 0),
                                self.po_lead,
                                subcontractor.get("priority", 1) + 50,
                                subcontractor.get("size_minimum", 0),
                                quoteattr(product_buf["name"]),
                                quoteattr(location),
                            )
                        else:
                            duration_per = self.product_templates[
                                i["product_tmpl_id"][0]
                            ]["produce_delay"]

                            yield '<operation name=%s size_multiple="1" duration_per="%s" posttime="P%dD" priority="%s" xsi:type="operation_time_per">\n' "<item name=%s/><location name=%s/>\n" % (
                                quoteattr(operation),
                                self.convert_float_time(duration_per)
                                if duration_per and duration_per > 0
                                else "P0D",
                                self.manufacturing_lead,
                                100 + (i["sequence"] or 1),
                                quoteattr(product_buf["name"]),
                                quoteattr(location),
                            )

                        # Handle produced quantity of a bom
                        producedQty = self.convert_qty_uom(
                            i["product_qty"],
                            i["product_uom_id"],
                            i["product_tmpl_id"][0],
                        )
                        if not producedQty:
                            producedQty = 1
                        if producedQty != 1:
                            yield "<size_minimum>%s</size_minimum>\n" % producedQty
                        yield "<flows>\n"

                        # Build consuming flows.
                        # If the same component is consumed multiple times in the same BOM
                        # we sum up all quantities in a single flow. We assume all of them
                        # have the same effectivity.
                        fl = {}
                        for j in self.generator.getData(
                            "mrp.bom.line",
                            ids=i["bom_line_ids"],
                            fields=[
                                "product_qty",
                                "product_uom_id",
                                "product_id",
                                "operation_id",
                                "bom_product_template_attribute_value_ids",
                            ],
                        ):
                            # check if this BOM line applies to this variant
                            if len(
                                j["bom_product_template_attribute_value_ids"]
                            ) > 0 and not all(
                                elem
                                in product_buf["product_template_attribute_value_ids"]
                                for elem in j[
                                    "bom_product_template_attribute_value_ids"
                                ]
                            ):
                                continue
                            product = self.product_product.get(j["product_id"][0], None)
                            if not product:
                                continue
                            if j["product_id"][0] in fl:
                                fl[j["product_id"][0]].append(j)
                            else:
                                fl[j["product_id"][0]] = [j]
                        for j in fl:
                            product = self.product_product[j]
                            qty = sum(
                                self.convert_qty_uom(
                                    k["product_qty"],
                                    k["product_uom_id"],
                                    self.product_product[k["product_id"][0]][
                                        "template"
                                    ],
                                )
                                for k in fl[j]
                            )
                            if qty > 0:
                                yield '<flow xsi:type="flow_start" quantity="-%f"><item name=%s/></flow>\n' % (
                                    qty / producedQty,
                                    quoteattr(product["name"]),
                                )

                        # Build byproduct flows
                        if i.get("sub_products", None):
                            for j in self.generator.getData(
                                "mrp.subproduct",
                                ids=i["sub_products"],
                                fields=[
                                    "product_id",
                                    "product_qty",
                                    "product_uom",
                                    "subproduct_type",
                                ],
                            ):
                                product = self.product_product.get(
                                    j["product_id"][0], None
                                )
                                if not product:
                                    continue
                                yield '<flow xsi:type="%s" quantity="%f"><item name=%s/></flow>\n' % (
                                    "flow_fixed_end"
                                    if j["subproduct_type"] == "fixed"
                                    else "flow_end",
                                    self.convert_qty_uom(
                                        j["product_qty"],
                                        j["product_uom"],
                                        j["product_id"][0],
                                    )
                                    / producedQty,
                                    quoteattr(product["name"]),
                                )
                        yield "</flows>\n"

                        # Create loads
                        if i["id"] and not subcontractor:
                            exists = False
                            for j in mrp_routing_workcenters.get(i["id"], []):
                                if (
                                    not j["workcenter_id"]
                                    or j["workcenter_id"][0] not in self.map_workcenters
                                ):
                                    continue
                                if not exists:
                                    exists = True
                                    yield "<loads>\n"
                                yield '<load quantity="%f" search=%s><resource name=%s/>%s</load>\n' % (
                                    j["time_cycle"],
                                    quoteattr(j["search_mode"]),
                                    quoteattr(
                                        self.map_workcenters[j["workcenter_id"][0]]
                                    ),
                                    ("<skill name=%s/>" % quoteattr(j["skill"][1]))
                                    if j["skill"]
                                    else "",
                                )
                                # create a load for secondary workcenters
                                # prepare the secondary workcenter xml string upfront
                                secondary_workcenter_str = ""
                                for sw_id in j["secondary_workcenter"]:
                                    secondary_workcenter = mrp_secondary_workcenter[
                                        sw_id
                                    ]
                                    yield '<load quantity="%f" search=%s><resource name=%s/>%s</load>' % (
                                        1
                                        if not secondary_workcenter["duration"]
                                        or j["time_cycle"] == 0
                                        else secondary_workcenter["duration"]
                                        / j["time_cycle"],
                                        quoteattr(secondary_workcenter["search_mode"]),
                                        quoteattr(
                                            self.map_workcenters[
                                                secondary_workcenter["workcenter_id"][0]
                                            ]
                                        ),
                                        (
                                            "<skill name=%s/>"
                                            % quoteattr(
                                                secondary_workcenter["skill"][1]
                                            )
                                        )
                                        if secondary_workcenter["skill"]
                                        else "",
                                    )

                            if exists:
                                yield "</loads>\n"
                    else:
                        #
                        # CASE 2: A routing operation is created with a suboperation for each
                        # routing step.
                        #
                        yield '<operation name=%s size_multiple="1" posttime="P%dD" priority="%s" xsi:type="operation_routing"><item name=%s/><location name=%s/>\n' % (
                            quoteattr(operation),
                            self.manufacturing_lead,
                            100 + (i["sequence"] or 1),
                            quoteattr(product_buf["name"]),
                            quoteattr(location),
                        )

                        # Handle produced quantity of a bom
                        producedQty = (
                            i["product_qty"]
                            * getattr(i, "product_efficiency", 1.0)
                            * uom_factor
                        )
                        if not producedQty:
                            producedQty = 1
                        if producedQty != 1:
                            yield "<size_minimum>%s</size_minimum>\n" % producedQty

                        yield "<suboperations>"

                        fl = {}
                        for j in self.generator.getData(
                            "mrp.bom.line",
                            ids=i["bom_line_ids"],
                            fields=[
                                "product_qty",
                                "product_uom_id",
                                "product_id",
                                "operation_id",
                                "bom_product_template_attribute_value_ids",
                            ],
                        ):
                            # check if this BOM line applies to this variant
                            if len(
                                j["bom_product_template_attribute_value_ids"]
                            ) > 0 and not all(
                                elem
                                in product_buf["product_template_attribute_value_ids"]
                                for elem in j[
                                    "bom_product_template_attribute_value_ids"
                                ]
                            ):
                                continue
                            product = self.product_product.get(j["product_id"][0], None)
                            if not product:
                                continue
                            qty = self.convert_qty_uom(
                                j["product_qty"],
                                j["product_uom_id"],
                                self.product_product[j["product_id"][0]]["template"],
                            )
                            if j["product_id"][0] in fl:
                                # If the same component is consumed multiple times in the same BOM
                                # we sum up all quantities in a single flow. We assume all of them
                                # have the same effectivity.
                                fl[j["product_id"][0]]["qty"] += qty
                            else:
                                j["qty"] = qty
                                fl[j["product_id"][0]] = j

                        steplist = mrp_routing_workcenters[i["id"]]
                        counter = 0
                        for step in steplist:
                            counter = counter + 1
                            suboperation = step["name"]
                            name = "%s - %s - %s" % (
                                operation,
                                suboperation,
                                step["id"],
                            )
                            if len(name) > 300:
                                suffix = " - %s - %s" % (
                                    suboperation,
                                    step["id"],
                                )
                                name = "%s%s" % (
                                    operation[: 300 - len(suffix)],
                                    suffix,
                                )
                            if (
                                not step["workcenter_id"]
                                or step["workcenter_id"][0] not in self.map_workcenters
                            ):
                                continue

                            # prepare the secondary workcenter xml string upfront
                            secondary_workcenter_str = ""
                            for sw_id in step["secondary_workcenter"]:
                                secondary_workcenter = mrp_secondary_workcenter[sw_id]
                                if (
                                    secondary_workcenter["workcenter_id"][0]
                                    not in self.map_workcenters
                                ):
                                    continue
                                secondary_workcenter_str += (
                                    '<load quantity="%f" search=%s><resource name=%s/>%s</load>'
                                    % (
                                        1
                                        if not secondary_workcenter["duration"]
                                        or step["time_cycle"] == 0
                                        else secondary_workcenter["duration"]
                                        / step["time_cycle"],
                                        quoteattr(secondary_workcenter["search_mode"]),
                                        quoteattr(
                                            self.map_workcenters[
                                                secondary_workcenter["workcenter_id"][0]
                                            ]
                                        ),
                                        (
                                            "<skill name=%s/>"
                                            % quoteattr(
                                                secondary_workcenter["skill"][1]
                                            )
                                        )
                                        if secondary_workcenter["skill"]
                                        else "",
                                    )
                                )

                            yield "<suboperation>" '<operation name=%s priority="%s" duration_per="%s" xsi:type="operation_time_per">\n' "<location name=%s/>\n" '<loads><load quantity="%f" search=%s><resource name=%s/>%s</load>%s</loads>\n' % (
                                quoteattr(name),
                                counter * 10,
                                self.convert_float_time(step["time_cycle"] / 1440.0)
                                if step["time_cycle"] and step["time_cycle"] > 0
                                else "P0D",
                                quoteattr(location),
                                1,
                                quoteattr(step["search_mode"]),
                                quoteattr(
                                    self.map_workcenters[step["workcenter_id"][0]]
                                ),
                                ("<skill name=%s/>" % quoteattr(step["skill"][1]))
                                if step["skill"]
                                else "",
                                secondary_workcenter_str,
                            )
                            first_flow = True
                            for j in fl.values():
                                if j["qty"] > 0 and (
                                    (
                                        j["operation_id"]
                                        and j["operation_id"][0] == step["id"]
                                    )
                                    or (not j["operation_id"] and step == steplist[0])
                                ):
                                    if first_flow:
                                        first_flow = False
                                        yield "<flows>\n"
                                    yield '<flow xsi:type="flow_start" quantity="-%f"><item name=%s/></flow>\n' % (
                                        j["qty"] / producedQty,
                                        quoteattr(
                                            self.product_product[j["product_id"][0]][
                                                "name"
                                            ]
                                        ),
                                    )
                            if not first_flow:
                                yield "</flows>\n"
                            yield "</operation></suboperation>\n"
                        yield "</suboperations>\n"
                    yield "</operation>\n"
        yield "</operations>\n"

    def export_salesorders(self):
        """
        Send confirmed sales order lines as demand to frePPLe, using the
        sale.order and sale.order.line models.

        Each order is linked to a warehouse, which is used as the location in
        frePPLe.

        Only orders in the status 'draft' and 'sale' are extracted.

        The picking policy 'complete' is supported at the sales order line
        level only in frePPLe. FrePPLe doesn't allow yet to coordinate the
        delivery of multiple lines in a sales order (except with hacky
        modeling construct).
        The field requested_date is only available when sale_order_dates is
        installed.

        Mapping:
        sale.order.name ' ' sale.order.line.id -> demand.name
        sales.order.requested_date -> demand.due
        '1' -> demand.priority
        [product.product.code] product.product.name -> demand.item
        sale.order.partner_id.name -> demand.customer
        convert sale.order.line.product_uom_qty and sale.order.line.product_uom  -> demand.quantity
        stock.warehouse.name -> demand->location
        (if sale.order.picking_policy = 'one' then same as demand.quantity else 1) -> demand.minshipment
        """
        # Get all sales order lines
        so_line = self.generator.getData(
            "sale.order.line",
            search=[("product_id", "!=", False)],
            fields=[
                "qty_delivered",
                "state",
                "product_id",
                "product_uom_qty",
                "product_uom",
                "order_id",
                "move_ids",
            ],
        )

        # Get all sales orders
        so = {
            i["id"]: i
            for i in self.generator.getData(
                "sale.order",
                ids=[j["order_id"][0] for j in so_line],
                fields=[
                    "state",
                    "partner_id",
                    "commitment_date",
                    "date_order",
                    "picking_policy",
                    "warehouse_id",
                ],
            )
        }

        # Get stock moves
        move_ids = []
        for i in so_line:
            if i["move_ids"]:
                move_ids.extend(i["move_ids"])
        moves = {
            i["id"]: i
            for i in self.generator.getData(
                "stock.move",
                ids=move_ids,
                fields=[
                    "state",
                    "date",
                    "product_uom_qty",
                    "quantity_done",
                    "warehouse_id",
                    "reserved_availability",
                ],
            )
        }

        # Generate the demand records
        yield "<!-- sales order lines -->\n"
        yield "<demands>\n"

        for i in so_line:
            name = "%s %d" % (i["order_id"][1], i["id"])
            batch = i["order_id"][1]
            product = self.product_product.get(i["product_id"][0], None)
            j = so[i["order_id"][0]]
            location = j["warehouse_id"][1]
            customer = self.map_customers.get(j["partner_id"][0], None)
            if not customer:
                # The customer may be an individual.
                # We check whether his/her company is in the map.
                for c in self.generator.getData(
                    "res.partner",
                    ids=[j["partner_id"][0]],
                    fields=["commercial_partner_id"],
                ):
                    customer = self.map_customers.get(
                        c["commercial_partner_id"][0], None
                    )
                    if customer:
                        break
            if not customer or not location or not product:
                # Not interested in this sales order...
                continue
            due = self.formatDateTime(
                j.get("commitment_date", False) or j["date_order"]
            )
            priority = 1  # We give all customer orders the same default priority

            # Possible sales order status are 'draft', 'sent', 'sale', 'done' and 'cancel'
            state = j.get("state", "sale")
            if state in ("draft", "sent"):
                # status = "inquiry"  # Inquiries don't reserve capacity and materials
                status = "quote"  # Quotes do reserve capacity and materials
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    self.product_product[i["product_id"][0]]["template"],
                )
            elif state == "sale":
                qty = i["product_uom_qty"] - i["qty_delivered"]
                if qty <= 0:
                    status = "closed"
                    qty = self.convert_qty_uom(
                        i["product_uom_qty"],
                        i["product_uom"],
                        self.product_product[i["product_id"][0]]["template"],
                    )
                else:
                    status = "open"
                    qty = self.convert_qty_uom(
                        qty,
                        i["product_uom"],
                        self.product_product[i["product_id"][0]]["template"],
                    )
            elif state in "done":
                status = "closed"
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    self.product_product[i["product_id"][0]]["template"],
                )
            elif state == "cancel":
                status = "canceled"
                qty = self.convert_qty_uom(
                    i["product_uom_qty"],
                    i["product_uom"],
                    self.product_product[i["product_id"][0]]["template"],
                )
            else:
                logger.warning("Unknown sales order state: %s." % (state,))
                continue

            if status == "open" and i["move_ids"]:
                # Use the delivery order info for open orders
                cnt = 1
                for mv_id in i["move_ids"]:
                    if moves[mv_id]["state"] in ("draft", "cancel", "done"):
                        continue
                    qty = self.convert_qty_uom(
                        moves[mv_id]["product_uom_qty"],
                        i["product_uom"],
                        self.product_product[i["product_id"][0]]["template"],
                    )
                    if self.respect_reservations and moves[mv_id]["state"] in (
                        "partially_available",
                        "assigned",
                    ):
                        qty -= moves[mv_id]["reserved_availability"]
                    if moves[mv_id]["date"]:
                        due = self.formatDateTime(moves[mv_id]["date"])
                    yield (
                        '<demand name=%s batch=%s quantity="%s" due="%s" priority="%s" minshipment="%s" status="%s"><item name=%s/><customer name=%s/><location name=%s/>'
                        # Enable only in frepple >= 6.25
                        # '<owner name=%s policy="%s" xsi:type="demand_group"/>'
                        "</demand>\n"
                    ) % (
                        quoteattr(
                            name
                            if cnt == 1
                            else "%s %d %d" % (i["order_id"][1], cnt, i["id"])
                        ),
                        quoteattr(batch),
                        qty,
                        due,
                        priority,
                        j["picking_policy"] == "one" and qty or 0.0,
                        status,
                        quoteattr(product["name"]),
                        quoteattr(customer),
                        quoteattr(location),
                        # Enable only in frepple >= 6.25
                        # quoteattr(i["order_id"][1]),
                        # "alltogether" if j["picking_policy"] == "one" else "independent",
                    )
                    cnt += 1
            else:
                # Use sales order line info
                yield (
                    '<demand name=%s batch=%s quantity="%s" due="%s" priority="%s" minshipment="%s" status="%s"><item name=%s/><customer name=%s/><location name=%s/>'
                    # Enable only in frepple >= 6.25
                    # '<owner name=%s policy="%s" xsi:type="demand_group"/>'
                    "</demand>\n"
                ) % (
                    quoteattr(name),
                    quoteattr(batch),
                    qty,
                    due,
                    priority,
                    j["picking_policy"] == "one" and qty or 0.0,
                    status,
                    quoteattr(product["name"]),
                    quoteattr(customer),
                    quoteattr(location),
                    # Enable only in frepple >= 6.25
                    # quoteattr(i["order_id"][1]),
                    # "alltogether" if j["picking_policy"] == "one" else "independent",
                )
        yield "</demands>\n"

    def export_forecasts(self):
        """
        IMPORTANT:
        Only use this in the frepple Enterprise and Cloud Editions.
        And only use it when the parameter "forecast.populateForecastTable" is set to false.

        Sends the list of forecasts to frepple based on odoo's sellable products.

        This method will need customization for each deployment.
        """
        yield "<!-- forecasts -->\n"
        yield "<demands>\n"
        for prod in self.product_product.values():
            if (
                not prod["template"]
                or not self.product_templates[prod["template"]]["sale_ok"]
            ):
                continue
            yield (
                '<demand name=%s planned="true" xsi:type="demand_forecast">'
                "<item name=%s/><location name=%s /><customer name=%s />"
                "<methods>%s</methods>"
                "</demand>"
            ) % (
                quoteattr(prod["name"]),
                quoteattr(prod["name"]),
                quoteattr("Chicago 1"),  # Edit to location name where to forecast
                quoteattr("All customers"),  # Edit to customer name to forecast for
                "manual",  # Values:   "manual" for user entered forecasts, "automatic" for calculating statistical forecasts
            )
        yield "</demands>\n"

    def export_purchaseorders(self):
        """
        Send all open purchase orders to frePPLe, using the purchase.order and
        purchase.order.line models.

        Only purchase order lines in state 'confirmed' are extracted. The state of the
        purchase order header must be "approved".

        Mapping:
        purchase.order.line.product_id -> operationplan.item
        purchase.order.company.mfg_location -> operationplan.location
        purchase.order.partner_id -> operationplan.supplier
        convert purchase.order.line.product_uom_qty - purchase.order.line.qty_received and purchase.order.line.product_uom -> operationplan.quantity
        purchase.order.date_planned -> operationplan.end
        purchase.order.date_planned -> operationplan.start
        'PO' -> operationplan.ordertype
        'confirmed' -> operationplan.status
        """
        po_line = {
            i["id"]: i
            for i in self.generator.getData(
                "purchase.order.line",
                search=[
                    "|",
                    (
                        "order_id.state",
                        "not in",
                        # Comment out on of the following alternative approaches:
                        # Alternative I: don't send RFQs to frepple because that supply isn't certain to be available yet.
                        ("draft", "sent", "bid", "to approve", "confirmed", "cancel"),
                        # Alternative II: send RFQs to frepple to avoid that the same purchasing proposal is generated again by frepple.
                        # ("bid", "confirmed", "cancel"),
                    ),
                    ("order_id.state", "=", False),
                ],
                fields=[
                    "name",
                    "date_planned",
                    "product_id",
                    "product_qty",
                    "qty_received",
                    "product_uom",
                    "order_id",
                    "state",
                    "move_ids",
                ],
            )
        }

        # Get all purchase orders
        po = {
            i["id"]: i
            for i in self.generator.getData(
                "purchase.order",
                ids=[j["order_id"][0] for j in po_line.values()],
                fields=["name", "company_id", "partner_id", "state", "date_order"],
            )
        }

        # Create purchasing operations from PO lines
        stock_move_ids = []
        yield "<!-- open purchase orders from PO lines -->\n"
        yield "<operationplans>\n"
        for i in po_line.values():
            if i["move_ids"]:
                # Use the stock move information rather than the po line
                stock_move_ids.extend(i["move_ids"])
                continue
            if not i["product_id"] or i["state"] == "cancel":
                continue
            item = self.product_product.get(i["product_id"][0], None)
            j = po[i["order_id"][0]]
            # if PO status is done, we should ignore this PO line
            if j["state"] == "done" or not item:
                continue
            location = self.mfg_location
            if location and item and i["product_qty"] > i["qty_received"]:
                start = self.formatDateTime(j["date_order"])
                end = self.formatDateTime(i["date_planned"])
                qty = self.convert_qty_uom(
                    i["product_qty"] - i["qty_received"],
                    i["product_uom"],
                    self.product_product[i["product_id"][0]]["template"],
                )
                yield '<operationplan reference=%s ordertype="PO" start="%s" end="%s" quantity="%f" status="confirmed">' "<item name=%s/><location name=%s/><supplier name=%s/></operationplan>\n" % (
                    quoteattr("%s - %s" % (j["name"], i["id"])),
                    start,
                    end,
                    qty,
                    quoteattr(item["name"]),
                    quoteattr(location),
                    quoteattr("%d %s" % (j["partner_id"][0], j["partner_id"][1])),
                )
        yield "</operationplans>\n"

        # Create purchasing operations from stock moves
        if stock_move_ids:
            yield "<!-- open purchase orders from PO receipts-->\n"
            yield "<operationplans>\n"
            for i in self.generator.getData(
                "stock.move",
                ids=stock_move_ids,
                fields=[
                    "state",
                    "product_id",
                    "product_qty",
                    "quantity_done",
                    "reference",
                    "product_uom",
                    "location_dest_id",
                    "origin",
                    "picking_id",
                    "date",
                    "purchase_line_id",
                ],
            ):
                if (
                    not i["product_id"]
                    or not i["purchase_line_id"]
                    or not i["location_dest_id"]
                    or i["state"] in ("draft", "cancel", "done")
                ):
                    continue
                item = self.product_product.get(i["product_id"][0], None)
                if not item:
                    continue
                j = po[po_line[i["purchase_line_id"][0]]["order_id"][0]]
                # if PO status is done, we should ignore this receipt
                if j["state"] == "done":
                    continue
                location = self.map_locations.get(i["location_dest_id"][0], None)
                if not location:
                    continue
                start = self.formatDateTime(j["date_order"])
                end = self.formatDateTime(i["date"])
                qty = i["product_qty"] - i["quantity_done"]
                if qty >= 0:
                    yield '<operationplan reference=%s ordertype="PO" start="%s" end="%s" quantity="%f" status="confirmed">' "<item name=%s/><location name=%s/><supplier name=%s/></operationplan>\n" % (
                        quoteattr(
                            "%s - %s - %s" % (j["name"], i["picking_id"][1], i["id"])
                        ),
                        start,
                        end,
                        qty,
                        quoteattr(item["name"]),
                        quoteattr(location),
                        quoteattr("%d %s" % (j["partner_id"][0], j["partner_id"][1])),
                    )
            yield "</operationplans>\n"

    def export_manufacturingorders(self):
        """
        Extracting work in progress to frePPLe, using the mrp.production model.

        We extract manufacturing orders in the states 'in_production' and 'confirmed', and
        which have a bom specified.

        Mapping:
        mrp.production.bom_id mrp.production.bom_id.name @ mrp.production.location_dest_id -> operationplan.operation
        convert mrp.production.product_qty and mrp.production.product_uom -> operationplan.quantity
        mrp.production.date_planned -> operationplan.start
        '1' -> operationplan.status = "confirmed"
        """
        now = datetime.now()
        yield "<!-- manufacturing orders in progress -->\n"
        yield "<operationplans>\n"
        for i in self.generator.getData(
            "mrp.production",
            # Option 1: import only the odoo status from "confirmed" onwards
            search=[("state", "in", ["progress", "confirmed", "to_close"])],
            # Option 2: Also import draft manufacturing order from odoo (to avoid that frepple reproposes it another time)
            # search=[("state", "in", ["draft", "progress", "confirmed", "to_close"])],
            fields=[
                "bom_id",
                "date_start",
                "date_planned_start",
                "date_planned_finished",
                "name",
                "state",
                "qty_producing",
                "product_qty",
                "product_uom_id",
                "location_dest_id",
                "product_id",
                "move_raw_ids",
            ]
            + ["workorder_ids"]
            if self.manage_work_orders
            else [],
        ):
            # Filter out irrelevant manufacturing orders
            location = self.map_locations.get(i["location_dest_id"][0], None)
            item = (
                self.product_product[i["product_id"][0]]
                if i["product_id"][0] in self.product_product
                else None
            )
            if not item or not location:
                continue

            # Odoo allows the data on the manufacturing orders and work orders to be
            # edited manually. The data can thus deviate from the information on the bill
            # materials.
            # To reflect this flexibility we need a frepple operation specific
            # to each manufacturing order.
            operation = i["name"]
            try:
                startdate = self.formatDateTime(
                    i["date_start"] if i["date_start"] else i["date_planned_start"]
                )
                # enddate = (
                #     i["date_planned_finished"]
                #     .astimezone(timezone(self.timezone))
                #     .strftime(self.timeformat)
                # )
            except Exception:
                continue
            qty = self.convert_qty_uom(
                i["qty_producing"] if i["qty_producing"] else i["product_qty"],
                i["product_uom_id"],
                self.product_product[i["product_id"][0]]["template"],
            )
            if not qty:
                continue

            # Create a record for the MO
            # Option 1: compute MO end date based on the start date
            yield '<operationplan type="MO" reference=%s start="%s" quantity="%s" status="%s">\n' % (
                quoteattr(i["name"]),
                startdate,
                qty,
                "approved",  # In the "approved" status, frepple can still reschedule the MO in function of material and capacity
                # "confirmed",  # In the "confirmed" status, frepple sees the MO as frozen and unchangeable
                # "approved" if i["status"]  == "confirmed" else "confirmed", # In-progress can't be rescheduled in frepple, but confirmed MOs
            )
            # Option 2: compute MO start date based on the end date
            # yield '<operationplan type="MO" reference=%s end="%s" quantity="%s" status="%s"><operation name=%s/><flowplans>\n' % (
            #     quoteattr(i["name"]),
            #     enddate,
            #     qty,
            #     # "approved",  # In the "approved" status, frepple can still reschedule the MO in function of material and capacity
            #     "confirmed",  # In the "confirmed" status, frepple sees the MO as frozen and unchangeable
            #     quoteattr(operation),
            # )

            # Collect work order info
            if self.manage_work_orders:
                wo_list = [
                    wo
                    for wo in self.generator.getData(
                        "mrp.workorder",
                        ids=i["workorder_ids"],
                        fields=[
                            "display_name",
                            "name",
                            "product_uom_id",
                            "working_state",
                            "state",
                            "workcenter_id",
                            "product_id",
                            "workcenter_id",
                            "date_planned_start",
                            "date_planned_finished",
                            "duration_expected",
                            "duration_unit",
                            "product_uom_id",
                            "production_availability",
                            "production_state",
                            "production_bom_id",
                            "state",
                            "is_user_working",
                            "time_ids",
                            "date_planned_start",
                            "date_planned_finished",
                            "date_start",
                            "date_finished",
                            "duration_expected",
                            "duration",
                            "duration_unit",
                            "duration_percent",
                            "progress",
                            "operation_id",
                            "move_raw_ids",
                            "move_finished_ids",
                            "move_line_ids",
                            "next_work_order_id",
                            "production_date",
                            "display_name",
                            "secondary_workcenters",
                        ],
                    )
                ]
            else:
                wo_list = []

            # Collect move info
            if i["move_raw_ids"]:
                mv_list = [
                    mv
                    for mv in self.generator.getData(
                        "stock.move",
                        ids=i["move_raw_ids"],
                        fields=[
                            "product_id",
                            "product_qty",
                            "product_uom",
                            "date",
                            "reference",
                            "workorder_id",
                            "should_consume_qty",
                            "reserved_availability",
                        ],
                    )
                ]
            else:
                mv_list = []

            if not wo_list:
                # There are no workorders on the manufacturing order
                yield '<operation name=%s xsi:type="operation_fixed_time"><location name=%s/><flows>' % (
                    quoteattr(operation),
                    quoteattr(self.map_locations[i["location_dest_id"][0]]),
                )
                for mv in mv_list:
                    item = (
                        self.product_product[mv["product_id"][0]]
                        if mv["product_id"][0] in self.product_product
                        else None
                    )
                    if not item:
                        continue
                    qty_flow = self.convert_qty_uom(
                        max(
                            0,
                            mv["product_qty"]
                            - (
                                mv["reserved_availability"]
                                if self.respect_reservations
                                else 0
                            ),
                        ),
                        mv["product_uom"],
                        self.product_product[mv["product_id"][0]]["template"],
                    )
                    yield '<flow quantity="%s"><item name=%s/></flow>\n' % (
                        -qty_flow / qty,
                        quoteattr(item["name"]),
                    )
                yield "</flows></operation></operationplan>"
            else:
                # Define an operation for the MO
                yield '<operation name=%s xsi:type="operation_routing" priority="0"><item name=%s/><location name=%s/><suboperations>' % (
                    quoteattr(operation),
                    quoteattr(item["name"]),
                    quoteattr(self.map_locations[i["location_dest_id"][0]]),
                )
                # Define operations for each WO
                idx = 10
                first_wo = True
                for wo in wo_list:
                    suboperation = wo["display_name"]
                    if len(suboperation) > 300:
                        suboperation = suboperation[0:300]

                    # Get remaining duration of the WO
                    time_left = wo["duration_expected"] - wo["duration_unit"]
                    if wo["is_user_working"] and wo["time_ids"]:
                        # The WO is currently being worked on
                        for tm in self.generator.getData(
                            "mrp.workcenter.productivity",
                            ids=wo["time_ids"],
                            fields=["date_start", "date_end"],
                        ):
                            if tm["date_start"] and not tm["date_end"]:
                                time_left -= round(
                                    (now - tm["date_start"]).total_seconds() / 60
                                )

                    yield '<suboperation><operation name=%s priority="%s" type="operation_fixed_time" duration="%s"><location name=%s/><flows>' % (
                        quoteattr(suboperation),
                        idx,
                        self.convert_float_time(
                            max(time_left, 1),  # Miniminum 1 minute remaining :-)
                            units="minutes",
                        ),
                        quoteattr(self.map_locations[i["location_dest_id"][0]]),
                    )
                    idx += 10
                    for mv in mv_list:
                        item = (
                            self.product_product[mv["product_id"][0]]
                            if mv["product_id"][0] in self.product_product
                            else None
                        )
                        if not item:
                            continue

                        # Skip moves of other WOs
                        if mv["workorder_id"]:
                            if mv["workorder_id"][0] != wo["id"]:
                                continue
                        elif not first_wo:
                            continue

                        qty_flow = self.convert_qty_uom(
                            max(
                                0,
                                mv["product_qty"]
                                - (
                                    mv["reserved_availability"]
                                    if self.respect_reservations
                                    else 0
                                ),
                            ),
                            mv["product_uom"],
                            self.product_product[mv["product_id"][0]]["template"],
                        )
                        yield '<flow quantity="%s"><item name=%s/></flow>\n' % (
                            -qty_flow / qty,
                            quoteattr(item["name"]),
                        )
                    yield "</flows>"
                    if (
                        wo["workcenter_id"]
                        and wo["workcenter_id"][0] in self.map_workcenters
                    ):
                        yield "<loads><load><resource name=%s/></load></loads>" % quoteattr(
                            self.map_workcenters[wo["workcenter_id"][0]]
                        )
                    if wo["secondary_workcenters"]:
                        yield "<loads>"
                        for secondary in self.generator.getData(
                            "mrp.workorder.secondary.workcenter",
                            ids=wo["secondary_workcenters"],
                            fields=["workcenter_id", "duration"],
                        ):
                            if (
                                secondary["workcenter_id"]
                                and secondary["workcenter_id"][0]
                                in self.map_workcenters
                                and secondary["workcenter_id"][0]
                                != wo["workcenter_id"][0]
                            ):
                                yield '<load quantity="%f"><resource name=%s/></load>' % (
                                    secondary["duration"] / wo["duration_expected"]
                                    if secondary["duration"] and wo["duration_expected"]
                                    else 1,
                                    quoteattr(
                                        self.map_workcenters[
                                            secondary["workcenter_id"][0]
                                        ]
                                    ),
                                )
                        yield "</loads>"
                    first_wo = False
                    yield "</operation></suboperation>"
                yield "</suboperations></operation></operationplan>"

                # Create operationplans for each WO, starting with the last one
                idx = 0
                for wo in reversed(wo_list):
                    idx += 1.0
                    suboperation = wo["display_name"]
                    if len(suboperation) > 300:
                        suboperation = suboperation[0:300]

                    # In the "approved" status, frepple can still reschedule the MO in function of material and capacity
                    # In the "confirmed" status, frepple sees the MO as frozen and unchangeable
                    if wo["state"] == "progress":
                        state = "confirmed"
                    elif wo["state"] in ("done", "to_close", "cancel"):
                        state = "completed"
                    else:
                        state = "approved"
                    try:
                        if wo["date_finished"]:
                            wo_date = ' end="%s"' % self.formatDateTime(
                                wo["date_finished"]
                            )
                        else:
                            if wo["is_user_working"]:
                                dt = now
                            else:
                                dt = max(
                                    wo["date_start"]
                                    if wo["date_start"]
                                    else wo["date_planned_start"]
                                    if wo["date_planned_start"]
                                    else i["date_planned_start"],
                                    now,
                                )
                            wo_date = ' start="%s"' % self.formatDateTime(dt)
                    except Exception as e:
                        wo_date = ""
                    yield '<operationplan type="MO" reference=%s%s quantity="%s" status="%s"><operation name=%s/><owner reference=%s/></operationplan>\n' % (
                        quoteattr(wo["display_name"]),
                        wo_date,
                        qty,
                        state,
                        quoteattr(suboperation),
                        quoteattr(i["name"]),
                    )
        yield "</operationplans>\n"

    def export_orderpoints(self):
        """
        Defining order points for frePPLe, based on the stock.warehouse.orderpoint
        model.

        Mapping:
        stock.warehouse.orderpoint.product.name ' @ ' stock.warehouse.orderpoint.location_id.name -> buffer.name
        stock.warehouse.orderpoint.location_id.name -> buffer.location
        stock.warehouse.orderpoint.product.name -> buffer.item
        convert stock.warehouse.orderpoint.product_min_qty -> buffer.mininventory
        convert stock.warehouse.orderpoint.product_max_qty -> buffer.maxinventory
        convert stock.warehouse.orderpoint.qty_multiple -> buffer->size_multiple
        """
        first = True
        for i in self.generator.getData(
            "stock.warehouse.orderpoint",
            fields=[
                "warehouse_id",
                "product_id",
                "product_min_qty",
                "product_max_qty",
                "product_uom",
                "qty_multiple",
            ],
        ):
            if first:
                yield "<!-- order points -->\n"
                yield "<calendars>\n"
                first = False
            item = self.product_product.get(
                i["product_id"] and i["product_id"][0] or 0, None
            )
            if not item:
                continue
            uom_factor = self.convert_qty_uom(
                1.0,
                i["product_uom"][0],
                self.product_product[i["product_id"][0]]["template"],
            )
            name = "%s @ %s" % (item["name"], i["warehouse_id"][1])
            if i["product_min_qty"]:
                yield """
                <calendar name=%s default="0"><buckets>
                <bucket start="2000-01-01T00:00:00" end="2030-01-01T00:00:00" value="%s" days="127" priority="998" starttime="PT0M" endtime="PT1440M"/>
                </buckets>
                </calendar>\n
                """ % (
                    (quoteattr("SS for %s" % (name,))),
                    (i["product_min_qty"] * uom_factor),
                )
            if i["product_max_qty"] - i["product_min_qty"] > 0:
                yield """
                <calendar name=%s default="0"><buckets>
                <bucket start="2000-01-01T00:00:00" end="2030-01-01T00:00:00" value="%s" days="127" priority="998" starttime="PT0M" endtime="PT1440M"/>
                </buckets>
                </calendar>\n
                """ % (
                    (quoteattr("ROQ for %s" % (name,))),
                    ((i["product_max_qty"] - i["product_min_qty"]) * uom_factor),
                )
        if not first:
            yield "</calendars>\n"

    def export_onhand(self):
        """
        Extracting all on hand inventories to frePPLe.

        We're bypassing the ORM for performance reasons.

        Mapping:
        stock.report.prodlots.product_id.name @ stock.report.prodlots.location_id.name -> buffer.name
        stock.report.prodlots.product_id.name -> buffer.item
        stock.report.prodlots.location_id.name -> buffer.location
        sum(stock.report.prodlots.qty) -> buffer.onhand
        """
        yield "<!-- inventory -->\n"
        yield "<operationplans>\n"
        if isinstance(self.generator, Odoo_generator):
            # SQL query gives much better performance
            self.generator.env.cr.execute(
                """
                SELECT stock_quant.product_id,
                stock_quant.location_id,
                sum(stock_quant.quantity) as quantity,
                sum(stock_quant.reserved_quantity) as reserved_quantity,
                stock_production_lot.name as lot_name,
                stock_production_lot.expiration_date
                FROM stock_quant
                left outer join stock_production_lot on stock_quant.lot_id = stock_production_lot.id
                and stock_production_lot.product_id = stock_quant.product_id
                WHERE quantity > 0
                GROUP BY stock_quant.product_id,
                stock_quant.location_id,
                stock_production_lot.name,
                stock_production_lot.expiration_date
                ORDER BY location_id ASC
                """
            )
            data = self.generator.env.cr.fetchall()
        else:
            data = [
                (i["product_id"][0], i["location_id"][0], i["quantity"])
                for i in self.generator.getData(
                    "stock.quant",
                    search=[("quantity", ">", 0)],
                    fields=[
                        "product_id",
                        "location_id",
                        "quantity",
                        "reserved_quantity",
                    ],
                )
                if i["product_id"] and i["location_id"]
            ]
        inventory = {}
        expirationdate = {}
        for i in data:
            item = self.product_product.get(i[0], None)
            location = self.map_locations.get(i[1], None)
            lotname = i[4]
            if item and location:
                inventory[(item["name"], location, lotname)] = max(
                    0,
                    inventory.get((item["name"], location, lotname), 0)
                    + i[2]
                    - (i[3] if self.respect_reservations else 0),
                )
                if i[5]:
                    expirationdate[(item["name"], location, lotname)] = i[5]
        for key, val in inventory.items():
            yield (
                """
            <operationplan ordertype="STCK" end="%s" reference=%s %s quantity="%s">
			<item name=%s/>
			<location name=%s/>
		    </operationplan>
            """
                % (
                    self.formatDateTime(datetime.now()),
                    quoteattr(
                        "STCK %s @ %s%s"
                        % (key[0], key[1], (" @ %s" % (key[2],)) if key[2] else "")
                    ),
                    ('expiry="%s"' % self.formatDateTime(expirationdate[key]))
                    if key in expirationdate
                    else "",
                    val or 0,
                    quoteattr(key[0]),
                    quoteattr(key[1]),
                )
            )
        yield "</operationplans>\n"


if __name__ == "__main__":
    #
    # When calling this script directly as a Python file, the connector uses XMLRPC
    # to connect to odoo and download all data.
    #
    # This is useful for debugging connector updates remotely, when you don't have
    # direct access to the odoo server itself.
    # This mode of working is not recommended for production use because of performance
    # considerations.
    #
    #  EXPERIMENTAL FEATURE!!!
    #
    import argparse

    parser = argparse.ArgumentParser(description="Debug frepple odoo connector")
    parser.add_argument(
        "--url", help="URL of the odoo server", default="http://localhost:8069"
    )
    parser.add_argument("--db", help="Odoo database to connect to", default="odoo14")
    parser.add_argument(
        "--username", help="User name for the odoo connection", default="admin"
    )
    parser.add_argument(
        "--password", help="User password for the odoo connection", default="admin"
    )
    parser.add_argument(
        "--company", help="Odoo company to use", default="My Company (Chicago)"
    )
    parser.add_argument(
        "--timezone", help="Time zone to convert odoo datetime fields to", default="UTC"
    )
    parser.add_argument(
        "--singlecompany",
        default=False,
        help="Limit the data to a single company only.",
        action="store_true",
    )
    args = parser.parse_args()

    generator = XMLRPC_generator(args.url, args.db, args.username, args.password)
    xp = exporter(
        generator,
        None,
        uid=generator.uid,
        database=generator.db,
        company=args.company,
        mode=1,
        timezone=args.timezone,
        singlecompany=True,
    )
    for i in xp.run():
        print(i, end="")
