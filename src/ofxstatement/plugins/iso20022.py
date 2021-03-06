import xml.etree.ElementTree as ET
import datetime
import re

from ofxstatement import exceptions
from ofxstatement.plugin import Plugin
from ofxstatement.statement import Statement, StatementLine


ISO20022_NAMESPACE_ROOT = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001'

CD_CREDIT = 'CRDT'
CD_DEBIT = 'DBIT'

class Iso20022Plugin(Plugin):
    """ISO-20022 plugin
    """

    def get_parser(self, filename):
        default_ccy = self.settings.get('currency')
        parser = Iso20022Parser(filename, currency=default_ccy)
        return parser


class Iso20022Parser(object):
    def __init__(self, filename, currency=None):
        self.filename = filename
        self.currency = currency

    def parse(self):
        """Main entry point for parsers
        """
        self.statement = Statement()
        self.statement.currency = self.currency
        tree = ET.parse(self.filename)

        # Find out XML namespace and make sure we can parse it
        ns = self._get_namespace(tree.getroot())
        if not ns.startswith(ISO20022_NAMESPACE_ROOT):
            raise exceptions.ParseError(0, "Cannot recognize ISO20022 XML")

        self.xmlns = {
            "s": ns
        }


        self._parse_statement_properties(tree)
        self._parse_lines(tree)

        return self.statement

    def _get_namespace(self, elem):
        m = re.match('\{(.*)\}', elem.tag)
        return m.groups()[0] if m else ''

    def _parse_statement_properties(self, tree):
        stmt = tree.find('./s:BkToCstmrStmt/s:Stmt', self.xmlns)

        bnk = stmt.find('./s:Acct/s:Svcr/s:FinInstnId/s:BIC', self.xmlns)
        if bnk is None:
            bnk = stmt.find('./s:Acct/s:Svcr/s:FinInstnId/s:Nm', self.xmlns)
        iban = stmt.find('./s:Acct/s:Id/s:IBAN', self.xmlns)
        ccy = stmt.find('./s:Acct/s:Ccy', self.xmlns)
        bals = stmt.findall('./s:Bal', self.xmlns)

        acctCurrency = ccy.text if ccy is not None else None
        if acctCurrency:
            self.statement.currency = acctCurrency
        else:
            if self.statement.currency is None:
                raise exceptions.ParseError(
                    0, "No account currency provided in statement. Please "
                    "specify one in configuration file (e.g. currency=EUR)")

        bal_amts = {}
        bal_dates = {}
        for bal in bals:
            cd = bal.find('./s:Tp/s:CdOrPrtry/s:Cd', self.xmlns)
            amt = bal.find('./s:Amt', self.xmlns)
            dt = bal.find('./s:Dt', self.xmlns)
            amt_ccy = amt.get('Ccy')
            # Amount currency should match with statement currency
            if amt_ccy != self.statement.currency:
                continue

            bal_amts[cd.text] = self._parse_amount(amt)
            bal_dates[cd.text] = self._parse_date(dt)

        if not bal_amts:
            raise exceptions.ParseError(
                0, "No statement balance found for currency '%s'. Check "
                "currency of statement file." % self.statement.currency)

        self.statement.bank_id = bnk.text if bnk is not None else None
        self.statement.account_id = iban.text
        self.statement.start_balance = bal_amts['OPBD']
        self.statement.start_date = bal_dates['OPBD']
        self.statement.end_balance = bal_amts['CLBD']
        self.statement.end_date = bal_dates['CLBD']

    def _parse_lines(self, tree):
        for ntry in self._findall(tree, 'BkToCstmrStmt/Stmt/Ntry'):
            sline = self._parse_line(ntry)
            if sline is not None:
                self.statement.lines.append(sline)

    def _parse_line(self, ntry):
        sline = StatementLine()

        crdeb = self._find(ntry, 'CdtDbtInd').text

        amtnode = self._find(ntry, 'Amt')
        amt_ccy = amtnode.get('Ccy')

        if amt_ccy != self.statement.currency:
            # We can't include amounts with incompatible currencies into the
            # statement.
            return None

        amt = self._parse_amount(amtnode)
        if crdeb == CD_DEBIT:
            amt = -amt
            payee = self._find(ntry, 'NtryDtls/TxDtls/RltdPties/Cdtr/Nm')
        else:
            payee = self._find(ntry, 'NtryDtls/TxDtls/RltdPties/Dbtr/Nm')

        sline.payee = payee.text if payee is not None else None
        sline.amount = amt

        dt = self._find(ntry, 'ValDt')
        sline.date = self._parse_date(dt)

        bookdt = self._find(ntry, 'BookgDt')
        sline.date_user = self._parse_date(bookdt)

        svcref = self._find(ntry, 'NtryDtls/TxDtls/Refs/AcctSvcrRef')
        if svcref is None:
            svcref = self._find(ntry, 'AcctSvcrRef')
        sline.refnum = svcref.text

        # Try to find memo from different possible locations
        rmtinf = self._find(ntry, 'NtryDtls/TxDtls/RmtInf/Ustrd')
        addinf = self._find(ntry, 'AddtlNtryInf')
        if rmtinf is not None:
            sline.memo = rmtinf.text
        elif addinf is not None:
            sline.memo = addinf.text

        return sline

    def _parse_date(self, dtnode):
        if dtnode is None:
            return None

        dt = self._find(dtnode, 'Dt')
        dttm = self._find(dtnode, 'DtTm')

        if dt is not None:
            dtvalue = self._notimezone(dt.text)
            return datetime.datetime.strptime(dtvalue, "%Y-%m-%d")
        else:
            assert dttm is not None
            dtvalue = self._notimezone(dttm.text)
            return datetime.datetime.strptime(dtvalue, "%Y-%m-%dT%H:%M:%S")

    def _notimezone(self, dt):
        # Sometimes we are getting time with ridiculous timezone, like
        # "2017-04-01+02:00", which is unparseable by any python parsers. Strip
        # out such timezone for good.
        if "+" not in dt:
            return dt
        dt, tz = dt.split("+")
        return dt

    def _parse_amount(self, amtnode):
        return float(amtnode.text)

    def _find(self, tree, spath):
        return tree.find(_toxpath(spath), self.xmlns)

    def _findall(self, tree, spath):
        return tree.findall(_toxpath(spath), self.xmlns)

def _toxpath(spath):
    tags = spath.split('/')
    path = ['s:%s' % t for t in tags]
    xpath = './%s' % '/'.join(path)
    return xpath

