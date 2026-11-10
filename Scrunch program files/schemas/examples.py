"""Synthetic contract fixtures only, never John's evaluation phrases."""
from copy import deepcopy


def proposal():
    return {'schema_version': 1, 'request_id': 'fixture-request',
            'target_identity': {'set_id': 'set-1', 'track_id': 'track-1', 'device_id': 'device-1',
                                'instance_id': 'instance-1', 'plugin_id': 'fixture-plugin'},
            'start_revision': 'revision-1', 'cancel_requested': False,
            'user_words': 'Synthetic numeric contract fixture', 'interpreted_intent': 'Exercise readback',
            'action': 'change', 'reference_target': None,
            'changes': [{'control_id': 'cutoff', 'field_kind': 'parameter', 'before': .5, 'after': .6,
                         'direction': 'increase', 'magnitude': .1,
                         'mapping': {'kind': 'range', 'source': 'fixture-host-metadata', 'unit': 'normalized',
                                     'minimum': 0, 'maximum': 1, 'values': []}}],
            'expected_measurable_effect': None, 'uncertainty': .2, 'question_required': False,
            'question': None, 'explanation_draft': 'Synthetic fixture only.',
            'undo_plan': 'Restore captured values after revision check.', 'no_action_reason': None}


def result():
    p = proposal()
    return {**{k: deepcopy(p[k]) for k in ('schema_version', 'request_id', 'target_identity',
                                          'start_revision', 'cancel_requested')},
            'result_id': 'fixture-result', 'end_revision': 'revision-2', 'status': 'verified',
            'prewrite_checks': {'target_matches': True, 'revision_matches': True, 'cancel_clear': True},
            'partial_write': False,
            'writes': [{'control_id': 'cutoff', 'before': .5, 'requested': .6,
                        'readback': .6, 'verified': True, 'error': None}],
            'restore_data': {'captured_before_write': True, 'state_revision': 'revision-1', 'values': {'cutoff': .5}},
            'measurements': [], 'inferences': [], 'failure_reason': None, 'latency_ms': 12, 'cost_usd': 0}


def move():
    p, r = proposal(), result()
    return {**{k: deepcopy(p[k]) for k in ('schema_version', 'request_id', 'target_identity',
                                          'start_revision', 'cancel_requested', 'changes')},
            'id': 'fixture-move', 'sequence': 1, 'evidence_ids': ['quote'],
            'partial_write': False, 'restore_data': deepcopy(r['restore_data']),
            'verified_result': r, 'created_at': '2026-09-23T08:00:00Z'}


def decision():
    return {'schema_version': 1, 'decision_id': 'fixture-decision', 'request_id': 'fixture-request',
            'result_id': 'fixture-result', 'move_id': 'fixture-move', 'decision': 'keep', 'source': 'user',
            'user_words': 'Synthetic keep fixture', 'evidence_ids': ['fixture-user-decision'],
            'auditioned': True, 'reason': None, 'created_at': '2026-09-23T08:01:00Z'}
