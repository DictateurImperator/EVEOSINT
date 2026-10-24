"""Offline country lookup/download tests; no network or visitor addresses stored."""
import gzip
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from scripts import test_killmail_forensics as f

geo=f.load('traffic_countries')


class CountryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=patch.object(geo,'ROOT',Path(self.tmp.name));self.root.start()
        self.reader=patch.object(geo,'_reader',None);self.reader.start()
        self.signature=patch.object(geo,'_signature',None);self.signature.start()

    def tearDown(self):
        self.signature.stop();self.reader.stop();self.root.stop();self.tmp.cleanup()

    def test_lookup_local_only_and_reload_atomically(self):
        import maxminddb
        (geo.ROOT/'country.mmdb').write_bytes(b'fixture')
        first=MagicMock();first.get.return_value={'country':{'iso_code':'FR'}}
        second=MagicMock();second.get.return_value={'country':{'iso_code':'DE'}}
        with patch.object(maxminddb,'open_database',side_effect=[first,second]) as opened,patch.object(geo,'urlopen',side_effect=AssertionError('No per-IP HTTP')):
            self.assertEqual(geo.country('8.8.8.8'),'FR')
            self.assertEqual(geo.country('2606:4700:4700::1111'),'FR')
            self.assertEqual(opened.call_count,1)
            (geo.ROOT/'replacement').write_bytes(b'new')
            (geo.ROOT/'replacement').replace(geo.ROOT/'country.mmdb')
            self.assertEqual(geo.country('8.8.8.8'),'DE')
            first.close.assert_called_once()
            self.assertEqual(opened.call_count,2)
            second.get.return_value={'country':{'iso_code':'<invalid>'}}
            self.assertEqual(geo.country('8.8.8.8'),'ZZ')
        for address in ('127.0.0.1','192.168.1.1','::1',None,'invalid'):
            self.assertEqual(geo.country(address),'ZZ')

    def test_corrupt_or_missing_database_never_breaks_request(self):
        self.assertEqual(geo.country('8.8.8.8'),'ZZ')
        (geo.ROOT/'country.mmdb').write_bytes(b'broken')
        self.assertEqual(geo.country('8.8.8.8'),'ZZ')

    def test_monthly_download_bounded_validated_no_visitor_information(self):
        import maxminddb
        opened=MagicMock();opened.__enter__.return_value.metadata.return_value.ip_version=6
        with patch.object(geo,'urlopen',return_value=io.BytesIO(gzip.compress(b'fixture database'))) as download,patch.object(maxminddb,'open_database',return_value=opened):
            geo.ensure_database()
            self.assertEqual(geo.status()['state'],'ready')
            self.assertEqual((geo.ROOT/'country.mmdb').read_bytes(),b'fixture database')
            request=download.call_args.args[0]
            self.assertRegex(request.full_url,r'^https://download\.db-ip\.com/free/dbip-country-lite-\d{4}-\d{2}\.mmdb\.gz$')
            self.assertIsNone(request.data)
            geo.ensure_database()
            self.assertEqual(download.call_count,1)
        with patch.object(geo,'urlopen',side_effect=OSError('Do not store this error')):
            geo.ensure_database(force=True)
        self.assertEqual(geo.status()['state'],'failed')
        self.assertEqual((geo.ROOT/'country.mmdb').read_bytes(),b'fixture database')
        self.assertNotIn('Do not store',(geo.ROOT/'status.json').read_text())
        self.assertFalse(list(geo.ROOT.glob('.country-*')))

    def test_monthly_release_fallback_only_for_not_found(self):
        import maxminddb
        opened=MagicMock();opened.__enter__.return_value.metadata.return_value.ip_version=6
        missing=HTTPError('https://download.db-ip.com/',404,'missing',None,None)
        with patch.object(geo,'urlopen',side_effect=[missing,io.BytesIO(gzip.compress(b'previous release'))]) as download,patch.object(maxminddb,'open_database',return_value=opened):
            geo.ensure_database()
            self.assertEqual(download.call_count,2)
            self.assertEqual(geo.status()['state'],'ready')


if __name__=='__main__':unittest.main()
