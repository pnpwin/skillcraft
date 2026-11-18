from pathlib import Path
import tempfile
import unittest

from scripts.grade import check_one
from scripts.utils import write_json


class SemanticChecks(unittest.TestCase):
    def test_multiset_order_duplicates_extras_types_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            check = {'text':'complete names','type':'json_multiset_equals','file':'result.json','path':['names'],'value':['Alpha','Beta']}
            for names, expected in [(['Beta','Alpha'],True),(['Alpha','Alpha'],False),(['Alpha'],False),
                                    (['Alpha','Beta','Gamma'],False),('Alpha',False)]:
                write_json(root/'result.json',{'names':names})
                self.assertEqual(check_one(root,check)['passed'],expected)
            check['value']=[True]
            write_json(root/'result.json',{'names':[1]})
            self.assertFalse(check_one(root,check)['passed'])
            (root/'result.json').unlink()
            self.assertFalse(check_one(root,check)['passed'])
            check['value']='invalid'
            with self.assertRaises(ValueError):check_one(root,check)
