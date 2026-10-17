"""Slots must reflect recorded flags, with charges classified independently of quantity."""
import unittest
from scripts.test_killmail_forensics import entities


def item(type_id, flag, quantity=1, group='high'):
    return dict(type_id=type_id,flag=flag,total_quantity=quantity,group_key=group,
                item={'name':f'Type {type_id}'},is_dropped=False)


class FittingTests(unittest.TestCase):
    def test_single_unit_charge_is_attached_to_matching_flag(self):
        gun=item(1,27);charge=item(2,27)
        band=entities._killmail_fitting_bands([gun,charge],{1:7,2:8})[0]
        self.assertEqual(band['slots'][0]['module'],gun)
        self.assertEqual(band['slots'][0]['charges'],[charge])
        self.assertTrue(charge['is_charge'])
        self.assertEqual(charge['slot_index'],gun['slot_index'])

    def test_empty_slot_is_not_compacted(self):
        first=item(1,27);third=item(3,29)
        slots=entities._killmail_fitting_bands([first,third],{1:7,3:7},{'high':5})[0]['slots']
        self.assertEqual(len(slots),5)
        self.assertEqual(slots[0]['module'],first)
        self.assertIsNone(slots[1]['module'])
        self.assertEqual(slots[2]['module'],third)
        self.assertIsNone(slots[4]['module'])

    def test_observed_fit_overrides_smaller_current_sde_slot_count(self):
        slots=entities._killmail_fitting_bands([item(1,34)],{1:7},{'high':5})[0]['slots']
        self.assertEqual(len(slots),8)
        self.assertEqual(slots[7]['module']['type_id'],1)

    def test_module_quantity_does_not_make_it_a_charge(self):
        module=item(1,27,3)
        slot=entities._killmail_fitting_bands([module],{1:7})[0]['slots'][0]
        self.assertEqual(slot['module'],module)
        self.assertEqual(slot['charges'],[])

    def test_rigs_subsystems_and_geometry_stay_inside_ring(self):
        bands=entities._killmail_fitting_bands([item(1,93,group='rig'),item(2,129,group='subsystem')],{1:7,2:32})
        self.assertEqual([(b['key'],len(b['slots'])) for b in bands],[('rig',2),('subsystem',5)])
        for band in bands:
            for slot in band['slots']:
                for key in ('x','y','charge_x','charge_y'):
                    self.assertGreater(slot[key],0)
                    self.assertLess(slot[key],100)

    def test_no_fit_can_still_show_known_empty_slots(self):
        self.assertEqual(entities._killmail_fitting_bands([]),[])
        self.assertEqual(len(entities._killmail_fitting_bands([],counts={'mid':4})[0]['slots']),4)
