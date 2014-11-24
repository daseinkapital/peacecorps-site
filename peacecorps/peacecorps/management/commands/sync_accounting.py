import csv
from datetime import datetime
import logging
import re

from django.core.management.base import BaseCommand, CommandError
import pytz

from peacecorps.models import (
    Account, Campaign, Country, Project, SectorMapping)


def datetime_from(text):
    """Convert a string representation of a date into a UTC datetime. We
    assume the incoming date is in Eastern and represents the last second of
    that day"""
    eastern = pytz.timezone("US/Eastern")
    time = datetime.strptime(text, "%d-%b-%y")
    time = time.replace(hour=23, minute=59, second=59)
    time = eastern.localize(time)
    return time.astimezone(pytz.utc)


def cents_from(text):
    """Convert a string of comma-separated dollars and decimal cents into an
    int of cents"""
    text = text.replace(",", "").strip()
    #   intentionally allow errors to break the script
    dollars = float(text)
    return int(round(dollars * 100))


class IssueCache(object):
    """Keeps track of all known issues, so that we do not need to hit the
    database with each request."""

    def __init__(self):
        self.issues = {m.accounting_name: m.campaign
                       for m in SectorMapping.objects.all()}

    def find(self, sector_name):
        if sector_name not in self.issues:
            # may have been added
            mapping = SectorMapping.objects.filter(pk=sector_name).first()
            if mapping:
                self.issues[mapping.accounting_name] = mapping.campaign
        return self.issues.get(sector_name)


def create_account(row, issue_map):
    """This is a new project/campaign. Determine the account type and create
    the appropriate project, country fund, etc."""
    acc_type = account_type(row)
    name = row['PROJ_NAME']
    if Account.objects.filter(name=name).first():
        name = name + ' (' + row['PROJ_CODE'] + ')'
    account = Account(name=name, code=row['PROJ_CODE'], category=acc_type)
    if acc_type == Account.PROJECT:
        create_pcpp(account, row, issue_map)
    else:   # Campaign
        account.save()
        campaign = Campaign.objects.create(
            name=name, account=account, campaigntype=acc_type,
            description=row['SUMMARY'])
        if acc_type == Account.SECTOR:
            # Make sure we remember the sector this is marked as
            SectorMapping.objects.create(pk=row['SECTOR'], campaign=campaign)


def create_pcpp(account, row, issue_map):
    """Create and save a project (and account). This is a bit more complex for
    projects, which have foal amounts, etc."""
    country_name = row['COUNTRY_NAME']
    country = Country.objects.filter(name__iexact=country_name).first()
    issue = issue_map.find(row['SECTOR'])
    if not country or not issue:
        logging.getLogger('peacecorps.sync_accounting').warning(
            "Either country or issue does not exist: %s, %s",
            row['COUNTRY_NAME'], row['SECTOR'])
    else:
        goal = cents_from(row['PROJ_REQUEST'])
        balance = cents_from(row['PROJ_BAL'])
        account.current = goal - balance
        account.goal = goal
        account.community_contribution = cents_from(row['COMM_CONTRIB'] or '0')
        account.save()

        volunteername = row['PCV_NAME']
        if volunteername.startswith(row['STATE']):
            volunteername = volunteername[len(row['STATE']):].strip()
        project = Project.objects.create(
            title=row['PROJ_NAME'], country=country, account=account,
            overflow=issue.account, volunteername=volunteername,
            volunteerhomestate=row['STATE'], description=row['SUMMARY']
        )
        project.campaigns.add(issue)


def update_account(row, account):
    """If an account already exists, synchronize the transactions and amount"""
    if row['LAST_UPDATED_FROM_PAYGOV']:
        updated_at = datetime_from(row['LAST_UPDATED_FROM_PAYGOV'])
        account.donations.filter(time__lte=updated_at).delete()
    if account.category == Account.PROJECT:
        goal = cents_from(row['PROJ_REQUEST'])
        balance = cents_from(row['PROJ_BAL'])
        account.current = goal - balance
        account.save()


def account_type(row):
    """Derive whether this account is a project, country fund, etc. by
    heuristics on the project code, sector, and other fields"""
    if row['PROJ_CODE'].endswith('-CFD') or (
            row['SECTOR'] == 'None' and row['PROJ_REQUEST'] == '0'
            and row['PCV_NAME'] == row['COUNTRY_NAME'] + ' COUNTRY FUND'):
        return Account.COUNTRY
    if (row['PROJ_CODE'].startswith('SPF-')
            and 'MEMORIAL' in row['PROJ_NAME'].upper()):
        return Account.MEMORIAL
    if row['PROJ_CODE'].startswith('SPF-') and (
            row['COUNTRY_NAME'] == 'D/OSP/GGM'
            or row['PROJ_NAME'].upper() == row['PCV_NAME'].upper()):
        return Account.SECTOR
    if re.match(r'[\d-]+', row['PROJ_CODE']) or row['COMM_CONTRIB']:
        return Account.PROJECT
    return Account.OTHER


class Command(BaseCommand):
    help = """Synchronize Account and Transactions with a CSV.
              Generally, this means deleting transactions and updating the
              amount field in the account."""

    def handle(self, *args, **kwargs):
        if len(args) == 0:
            raise CommandError("Missing path to csv")

        issue_map = IssueCache()
        logger = logging.getLogger('peacecorps.sync_accounting')

        with open(args[0], encoding='iso-8859-1') as csvfile:
            # Column names will no doubt change
            for row in csv.DictReader(csvfile):
                account = Account.objects.filter(
                    code=row['PROJ_CODE']).first()
                if account:
                    logger.info(
                        'Updating %s, new balance: %s / %s', row['PROJ_CODE'],
                        row['PROJ_BALANCE'], row['PROJ_REQUEST'])
                    update_account(row, account)
                else:
                    logger.info('Creating %s', row['PROJ_CODE'])
                    create_account(row, issue_map)
