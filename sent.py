#!/usr/bin/env python3

import flask
import babel.dates
import json
import sys
import statistics
import string
import time
import base64
import oxenc
import sqlite3
import re
from typing import Callable, Any, Union
from functools import partial
from base64 import b32encode, b16decode
from werkzeug.routing import BaseConverter
from werkzeug.local import LocalProxy
from pygments import highlight
from pygments.lexers import JsonLexer
from pygments.formatters import HtmlFormatter
import nacl.hash
import nacl.bindings as sodium
from nacl.signing import VerifyKey
import eth_utils
import subprocess
import qrcode
from io import BytesIO
import config
from omq import FutureJSON, omq_connection
from timer import timer
import datetime

from contracts.reward_rate_pool import RewardRatePoolInterface
from contracts.service_node_contribution import ContributorContractInterface
from contracts.service_node_contribution_factory import ServiceNodeContributionFactory


# Make a dict of config.* to pass to templating
conf = {x: getattr(config, x) for x in dir(config) if not x.startswith("__")}

git_rev = subprocess.run(
    ["git", "rev-parse", "--short=9", "HEAD"], stdout=subprocess.PIPE, text=True
)
if git_rev.returncode == 0:
    git_rev = git_rev.stdout.strip()
else:
    git_rev = "(unknown)"

def create_app():
    app = flask.Flask(__name__)
    app.reward_rate_pool = RewardRatePoolInterface(config.PROVIDER_ENDPOINT, config.REWARD_RATE_POOL_ADDRESS)
    app.service_node_contribution_factory = ServiceNodeContributionFactory(config.PROVIDER_ENDPOINT, config.SERVICE_NODE_CONTRIBUTION_FACTORY_ADDRESS)
    app.service_node_contribution = ContributorContractInterface(config.PROVIDER_ENDPOINT)

    return app

app = create_app()

def get_sql():
    if "db" not in flask.g:
        flask.g.sql = sqlite3.connect(config.sqlite_db)

    return flask.g.sql


# Validates that input is 64 hex bytes and converts it to 32 bytes.
class Hex64Converter(BaseConverter):
    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = "[0-9a-fA-F]{64}"

    def to_python(self, value):
        return bytes.fromhex(value)

    def to_url(self, value):
        return value.hex()


eth_re = "0x[0-9a-fA-F]{40}"


class EthConverter(BaseConverter):
    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = eth_re


b58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
oxen_wallet_re = (
    f"T[{b58}]{{96}}" if config.testnet
    else f"dV[{b58}]{{95}}" if config.devnet
    else f"ST[{b58}]{{95}}" if config.stagenet
    else f"L[{b58}]{{94}}"
)


class OxenConverter(BaseConverter):
    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = oxen_wallet_re


class OxenEthConverter(BaseConverter):
    def __init__(self, url_map):
        super().__init__(url_map)
        self.regex = f"{eth_re}|{oxen_wallet_re}"


app.url_map.converters["hex64"] = Hex64Converter
app.url_map.converters["ethwallet"] = EthConverter
app.url_map.converters["oxenwallet"] = OxenConverter
app.url_map.converters["eitherwallet"] = OxenEthConverter


def get_sns_future(omq, oxend):
    return FutureJSON(
        omq,
        oxend,
        "rpc.get_service_nodes",
        args={
            "all": False,
            "fields": {
                x: True
                for x in (
                    "service_node_pubkey",
                    "requested_unlock_height",
                    "active",
                    "bls_key",
                    "funded",
                    "earned_downtime_blocks",
                    "service_node_version",
                    "contributors",
                    "total_contributed",
                    "total_reserved",
                    "staking_requirement",
                    "portions_for_operator",
                    "operator_address",
                    "pubkey_ed25519",
                    "last_uptime_proof",
                    "state_height",
                    "swarm_id",
                    "is_removable",
                    "is_liquidatable",
                )
            },
        },
    )


def get_sns(sns_future, info_future):
    info = info_future.get()
    awaiting_sns, active_sns, inactive_sns = [], [], []
    sn_states = sns_future.get()
    sn_states = (
        sn_states["service_node_states"] if "service_node_states" in sn_states else []
    )
    for sn in sn_states:
        sn["contribution_open"] = sn["staking_requirement"] - sn["total_reserved"]
        sn["contribution_required"] = (
            sn["staking_requirement"] - sn["total_contributed"]
        )
        sn["num_contributions"] = sum(
            len(x["locked_contributions"])
            for x in sn["contributors"]
            if "locked_contributions" in x
        )

        if sn["active"]:
            active_sns.append(sn)
        elif sn["funded"]:
            sn["decomm_blocks_remaining"] = max(sn["earned_downtime_blocks"], 0)
            sn["decomm_blocks"] = info["height"] - sn["state_height"]
            inactive_sns.append(sn)
        else:
            awaiting_sns.append(sn)
    return awaiting_sns, active_sns, inactive_sns


def hexify(container):
    """
    Takes a dict or list and mutates it to change any `bytes` values in it to str hex representation
    of the bytes, recursively.
    """
    if isinstance(container, dict):
        it = container.items()
    elif isinstance(container, list):
        it = enumerate(container)
    else:
        return

    for i, v in it:
        if isinstance(v, bytes):
            container[i] = v.hex()
        else:
            hexify(v)


# FIXME: this staking requirement value is just a placeholder for now.  We probably also want to
# expose and retrieve this from oxend rather than hard coding it here.
MAX_STAKE = 120_000000000
MIN_OP_STAKE = MAX_STAKE // 4
MAX_STAKERS = 10
TOKEN_NAME = "SENT"


def get_info():
    omq, oxend = omq_connection()
    info = FutureJSON(omq, oxend, "rpc.get_info").get()
    return {
        **{
            k: v
            for k, v in info.items()
            if k in ("nettype", "hard_fork", "height", "top_block_hash", "version")
        },
        "staking_requirement": MAX_STAKE,
        "min_operator_stake": MIN_OP_STAKE,
        "max_stakers": MAX_STAKERS,
    }


def json_response(vals):
    """
    Takes a dict, adds some general info fields to it, and jsonifies it for a flask route function
    return value.  The dict gets passed through `hexify` first to convert any bytes values to hex.
    """

    hexify(vals)

    return flask.jsonify({**vals, "network": get_info(), "t": time.time()})

@timer(10, target="worker1")
def fetch_contribution_contracts(signum):
    app.logger.warning(f"Fetch contribution contracts start - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    with app.app_context(), get_sql() as sql:
        cursor = sql.cursor()

        new_contracts = app.service_node_contribution_factory.get_latest_contribution_contract_events()

        for event in new_contracts:
            contract_address = event.args.contributorContract
            cursor.execute(
                """
                INSERT INTO contribution_contracts (contract_address) VALUES (?)
                ON CONFLICT (contract_address) DO NOTHING
                """,
                (contract_address,)
            )
        sql.commit()
    app.logger.warning(f"Fetch contribution contracts finish - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")


@timer(30)
def update_contract_statuses(signum):
    app.logger.warning(f"Update Contract Statuses Start - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    with app.app_context(), get_sql() as sql:
        cursor = sql.cursor()
        cursor.execute("SELECT contract_address FROM contribution_contracts")
        contract_addresses = cursor.fetchall()
        app.contributors = {}
        app.contracts = {}

        for (contract_address,) in contract_addresses:
            contract_interface = app.service_node_contribution.get_contract_instance(contract_address)

            # Fetch statuses and other details
            is_finalized = contract_interface.is_finalized()
            is_cancelled = contract_interface.is_cancelled()
            bls_pubkey = contract_interface.get_bls_pubkey()
            service_node_params = contract_interface.get_service_node_params()
            #contributor_addresses = contract_interface.get_contributor_addresses()
            total_contributions = contract_interface.total_contribution()
            contributions = contract_interface.get_individual_contributions()

            app.contracts[contract_address] = {
                'finalized': is_finalized,
                'cancelled': is_cancelled,
                'bls_pubkey': bls_pubkey,
                'fee': service_node_params['fee'],
                'service_node_pubkey': service_node_params['serviceNodePubkey'],
                'service_node_signature': service_node_params['serviceNodeSignature'],
                'contributions': [
                    {"address": addr, "amount": amt} for addr, amt in contributions.items()
                ],
                'total_contributions': total_contributions,
            }

            for address in contributions.keys():
                wallet_key = eth_format(address)
                if address not in app.contributors:
                    app.contributors[wallet_key] = []
                if contract_address not in app.contributors[wallet_key]:
                    app.contributors[wallet_key].append(contract_address)

    app.logger.warning(f"Update Contract Statuses Finish - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")


@timer(10)
def update_service_nodes(signum):
    app.logger.warning(f"Update Service Nodes Start - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
    omq, oxend            = omq_connection()
    app.nodes             = get_sns_future(omq, oxend).get()["service_node_states"]
    app.node_contributors = {}
    app.logger.warning(f"Update Service Nodes nodes in, looping- {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

    for index, node in enumerate(app.nodes):
        contributors = {c["address"]: c["amount"] for c in node["contributors"]}

        for address in contributors.keys():
            wallet_key = address
            if len(address) == 40:
                wallet_key = eth_format(wallet_key)

            if wallet_key not in app.node_contributors:
                app.node_contributors[wallet_key] = []
            app.node_contributors[wallet_key].append(index)

    app.logger.warning(f"Update Service Nodes finished - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

@app.route("/info")
def network_info():
    """
    Do-nothing endpoint that can be called to get just the "network" and "t" values that are
    included in every actual endpoint when you don't have any other endpoint to invoke.
    """
    return json_response({})



# export enum NODE_STATE {
  # RUNNING = 'Running',
  # AWAITING_CONTRIBUTORS = 'Awaiting Contributors',
  # CANCELLED = 'Cancelled',
  # DECOMMISSIONED = 'Decommissioned',
  # DEREGISTERED = 'Deregistered',
  # VOLUNTARY_DEREGISTRATION = 'Voluntary Deregistration',
# }
@app.route("/nodes/<oxenwallet:oxen_wal>")
@app.route("/nodes/<ethwallet:eth_wal>")
def get_nodes_for_wallet(oxen_wal=None, eth_wal=None):
    assert oxen_wal is not None or eth_wal is not None
    wallet         = eth_format(eth_wal) if eth_wal is not None else oxen_wal

    sns   = []
    nodes = []
    if hasattr(app, 'node_contributors') and wallet in app.node_contributors:
        for index in app.node_contributors[wallet]:
            node    = app.nodes[index]
            sns.append(node)
            balance = {c["address"]: c["amount"] for c in node["contributors"]}.get(wallet, 0)
            state   = 'Decommissioned' if not node["active"] and node["funded"] else 'Running'
            nodes.append({
                'balance':                 balance,
                'contributors':            node["contributors"],
                'last_uptime_proof':       node["last_uptime_proof"],
                'operator_address':        node["operator_address"],
                'operator_fee':            node["portions_for_operator"],
                'requested_unlock_height': node["requested_unlock_height"],
                'service_node_pubkey':     node["pubkey_ed25519"],
                'state':                   state,
            })

    contracts = []
    if hasattr(app, 'contributors') and wallet in app.contributors:
        for address in app.contributors[wallet]:
            details = app.contracts[address]
            contracts.append({
                'contract_address': address,
                'details': details
            })
            if details["finalized"]:
                continue
            state = 'Cancelled' if details["cancelled"] else 'Awaiting Contributors'
            nodes.append({
                'balance':                 details["contributions"].get(wallet, 0),
                'contributors':            details["contributions"],
                'last_uptime_proof':       0,
                'operator_address':        details["contributor_addresses"][0],
                'operator_fee':            details["service_node_params"]["fee"],
                'requested_unlock_height': 0,
                'service_node_pubkey':     details["service_node_params"]["serviceNodePubkey"],
                'state':                   state,
            })

    return json_response({"service_nodes": sns, "contracts": contracts, "nodes": nodes})

@app.route("/nodes/liquidatable")
def get_liquidatable_nodes():
    omq, oxend = omq_connection()
    sns = [
        sn
        for sn in get_sns_future(omq, oxend).get()["service_node_states"]
        if sn["is_liquidatable"]
    ]

    return json_response({"nodes": sns})

@app.route("/nodes/removeable")
def get_removable_nodes():
    omq, oxend = omq_connection()
    sns = [
        sn
        for sn in get_sns_future(omq, oxend).get()["service_node_states"]
        if sn["is_removeable"]
    ]

    return json_response({"nodes": sns})

@app.route("/nodes/open")
def get_contributable_contracts():
    return json_response({
        "nodes": [
            {
                "contract": addr,
                **details
            }
            for addr, details in app.contracts.items()
            if not details['finalized'] and not details['cancelled']
            # FIXME: we should also filter out reserved contracts
        ]
    })


# Decodes `x` into a bytes of length `length`.  `x` should be hex or base64 encoded, without
# whitespace.  Both regular and "URL-safe" base64 are accepted.  Padding is optional for base64
# values.  Throws ParseError if the input is invalid or of the wrong size.  `length` must be at
# least 5 (smaller byte values are harder or even ambiguous to distinguish between hex and base64).
def decode_bytes(k, x, length):
    assert length >= 5

    hex_len = length * 2
    b64_unpadded = (length * 4 + 2) // 3
    b64_padded = (length + 2) // 3 * 4

    print(f"DEBGUG: {len(x)}, {hex_len}")
    if len(x) == hex_len and all(c in string.hexdigits for c in x):
        return bytes.fromhex(x)
    if len(x) in (b64_unpadded, b64_padded):
        if oxenc.is_base64(x):
            return oxenc.from_base64(x)
        if "-" in x or "_" in x:  # Looks like (maybe) url-safe b64
            x = x.replace("/", "_").replace("+", "-")
        if oxenc.is_base64(x):
            return oxenc.from_base64(x)
    raise ParseError(k, f"expected {hex_len} hex or {b64_unpadded} base64 characters")


def byte_decoder(length: int):
    return partial(decode_bytes, length=length)


# Takes a positive integer value required to be between irange[0] and irange[1], inclusive.  The
# integer may not be 0-prefixed or whitespace padded.
def parse_int_field(k, v, irange):
    if (
        len(v) == 0
        or not all(c in "0123456789" for c in v)
        or (len(v) > 1 and v[0] == "0")
    ):
        raise ParseError(k, "an integer value is required")
    v = int(v)
    imin, imax = irange
    if imin <= v <= imax:
        return v
    raise ParseError(k, f"expected an integer between {imin} and {imax}")


def raw_eth_addr(k, v):
    if re.fullmatch(eth_re, v):
        if not eth_utils.is_address(v):
            raise ParseError(k, "ETH address checksum failed")
        return bytes.fromhex(v[2:])
    raise ParseError(k, "not an ETH address")


def eth_format(addr: Union[bytes, str]):
    try:
        return eth_utils.to_checksum_address(addr)
    except ValueError:
        raise ParseError("Invalid ETH address")


class SNSignatureValidationError(ValueError):
    pass


def check_reg_keys_sigs(params):
    if len(
        params["pubkey_ed25519"]
    ) != 32 or not sodium.crypto_core_ed25519_is_valid_point(params["pubkey_ed25519"]):
        raise SNSignatureValidationError("Ed25519 pubkey is invalid")
    if len(params["pubkey_bls"]) != 64:  # FIXME: bls pubkey validation?
        raise SNSignatureValidationError("BLS pubkey is invalid")
    if len(params["operator"]) != 20:
        raise SNSignatureValidationError("operator address is invalid")
    contract = params.get("contract")
    if contract is not None and len(contract) != 20:
        raise SNSignatureValidationError("contract address is invalid")

    signed = (
        params["pubkey_ed25519"]
        + params["pubkey_bls"]
    )

    try:
        VerifyKey(params["pubkey_ed25519"]).verify(signed, params["sig_ed25519"])
    except nacl.exceptions.BadSignatureError:
        raise SNSignatureValidationError("Ed25519 signature is invalid")

    # FIXME: BLS verification of pubkey_bls on signed
    if False:
        raise SNSignatureValidationError("BLS signature is invalid")


class ParseError(ValueError):
    def __init__(self, field, reason):
        self.field = field
        super().__init__(f"{field}: {reason}")


class ParseMissingError(ParseError):
    def __init__(self, field):
        super().__init__(field, f"required parameter is missing")


class ParseUnknownError(ParseError):
    def __init__(self, field):
        super().__init__(field, f"unknown parameter")


class ParseMultipleError(ParseError):
    def __init__(self, field):
        super().__init__(field, f"cannot be specified multiple times")


def parse_query_params(params: dict[str, Callable[[str, str], Any]]):
    """
    Takes a dict of fields and callables such as:

        {
            "field": ("out", callable),
            ...
        }

    where:
    - `"field"` is the expected query string name
    - `callable` will be invoked as `callable("field", value)` to determined the returned value.

    On error, throws a ParseError with `.field` set to the "field" name that triggered the error.

    Notes:
    - callable should throw a ParseError for an unaccept input value.
    - if "-field" starts with "-" then the field is optional; otherwise it is an error if not
      provided.  The "-" is not included in the returned key.
    - if "field" ends with "[]" then the value will be an array of values returned by the callable,
      and the parameter can be specified multiple times.  Otherwise a value can be specified only
      once.  The "[]" is not included in the returned key.
    - you can do both of the above: "-field[]" will allow the value to be provided zero or more
      times; the value will be omitted if not present in the input, and an array (under the "field")
      key if provided at least once.
    """

    parsed = {}

    param_map = {
        k.removeprefix("-").removesuffix("[]"): (
            k.startswith("-"),
            k.endswith("[]"),
            cb,
        )
        for k, cb in params.items()
    }

    for k, v in flask.request.values.items(multi=True):
        found = param_map.get(k)
        if found is None:
            raise ParseUnknownError(k)

        _, multi, callback = found

        if multi:
            parsed.setdefault(k, []).append(callback(k, v) if callback else v)
        elif k not in parsed:
            parsed[k] = callback(k, v) if callback else v
        else:
            raise ParseMultipleError(k)

    for k, p in param_map.items():
        optional = p[0]
        if not optional and k not in flask.request.values:
            raise ParseMissingError(k)

    return parsed


@app.route("/store/<hex64:sn_pubkey>", methods=["GET", "POST"])
def store_registration(sn_pubkey: bytes):
    """
    Stores (or replaces) the pubkeys/signatures associated with a service node that are needed to
    call the smart contract to create a SN registration.  These pubkeys/signatures are stored
    indefinitely, allowing the operator to call them up whenever they like to re-submit a
    registration for the same node.  There is nothing confidential here: the values will be publicly
    broadcast as part of the registration process already, and are constructed in such a way that
    only the operator wallet can submit a registration using them.

    This works for both solo registrations and multi-registrations: for the latter, a contract
    address is passed in the "c" parameter.  If omitted, the details are stored for a solo
    registration.  (One of each may be stored at a time for each pubkey).

    The distinction at the SN layer is that contract registrations sign the contract address while
    solo registrations sign the operator address.  For submission to the blockchain, a contract
    stake requires an additional interaction through a multi-contributor contract while solo
    registrations can call the staking contract directly.
    """

    try:
        params = parse_query_params(
            {
                "pubkey_bls": byte_decoder(64),
                "sig_ed25519": byte_decoder(64),
                "sig_bls": byte_decoder(128),
                "-contract": raw_eth_addr,
                "operator": raw_eth_addr,
            }
        )

        params["pubkey_ed25519"] = sn_pubkey

        check_reg_keys_sigs(params)
    except ValueError as e:
        raise e
        return json_response({"error": f"Invalid registration: {e}"})

    with get_sql() as sql:
        cur = sql.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO registrations (pubkey_ed25519, pubkey_bls, sig_ed25519, sig_bls, operator, contract)
                                          VALUES (?,              ?,          ?,           ?,       ?,        ?)
            """,
            (
                sn_pubkey,
                params["pubkey_bls"],
                params["sig_ed25519"],
                params["sig_bls"],
                params["operator"],
                params.get("contract"),
            ),
        )

    params["operator"] = eth_utils.to_checksum_address(params["operator"])
    if "contract" in params:
        params["contract"] = eth_utils.to_checksum_address(params["contract"])
        params["type"] = "contract"
    else:
        params["type"] = "solo"

    return json_response({"success": True, "registration": params})


@app.route("/registrations/<hex64:sn_pubkey>")
def load_registrations(sn_pubkey: bytes):
    """
    Retrieves stored registration(s) for the given service node pubkey.

    This returns an array in the "registrations" field containing either one or two registration
    info dicts: a solo registration (if known) and a multi-contributor contract registration (if
    known).  These are sorted by timestamp of when the registration was last received/updated.

    Fields in each dict:
    - "type": either "solo" or "contract"
    - "operator": the operator address; for "type": "contract" this is merely informative, for
      "type": "solo" this is a signed part of the registration.
    - "contract": the contract address, for "type": "contract" and omitted for "type": "solo".
    - "pubkey_ed25519": the primary SN pubkey, in hex.
    - "pubkey_bls": the SN BLS pubkey, in hex.
    - "sig_ed25519": the SN pubkey signed registration signature.
    - "sig_bls": the SN BLS pubkey signed registration signature.
    - "timestamp": the unix timestamp when this registration was received (or last updated)

    Returns a 404 Not Found error if no registrations for the pubkey are known at all.
    """

    regs = []

    with get_sql() as sql:
        cur = sql.cursor()
        cur.execute(
            """
            SELECT pubkey_bls, sig_ed25519, sig_bls, operator, contract, timestamp
            FROM registrations
            WHERE pubkey_ed25519 = ?
            ORDER BY timestamp DESC
            """,
            (sn_pubkey,),
        )
        for pk_bls, sig_ed, sig_bls, op, contract, timestamp in cur:
            params = {
                "type": "solo" if contract is None else "contract",
                "pubkey_ed25519": sn_pubkey,
                "pubkey_bls": pk_bls,
                "sig_ed25519": sig_ed,
                "sig_bls": sig_bls,
                "operator": op,
                "timestamp": timestamp,
            }

            if contract is not None:
                params["contract"] = contract

            regs.append(params)

    if not regs:
        return flask.abort(404)

    return json_response({"registrations": regs})

@app.route("/registrations/<ethwallet:op>")
def operator_registrations(op: bytes):
    """
    Retrieves stored registration(s) with the given operator.

    This returns an array in the "registrations" field containing as many registrations as are
    current stored for the given operator wallet, sorted from most to least recently submitted.

    Fields are the same as the version of this endpoint that takes a SN pubkey.

    Returns the JSON response with the 'registrations' for the given 'op'.
    """

    reg_array   = []
    op          = bytes.fromhex(op[2:])

    with get_sql() as sql:
        cur = sql.cursor()
        cur.execute(
            """
            SELECT pubkey_ed25519, pubkey_bls, sig_ed25519, sig_bls, contract, timestamp
            FROM registrations
            WHERE operator = ?
            ORDER BY timestamp DESC
            """,
            (op,),
        )
        for sn_pubkey, pk_bls, sig_ed, sig_bls, contract, timestamp in cur:

            # TODO: Fixme super janky. I feel like we should just have
            # hash-tables everywhere and so we should be able to lookup a node
            # by key and see if it's funded and immediately opt out. We also
            # need to actually prune the registrations list when it's funded or
            # cancelled ...
            if hasattr(app, 'nodes'):
                for node in app.nodes:
                    print('pubkey_ed25519: ', node['pubkey_ed25519'])
                    print('sn_pubkey: ',      sn_pubkey)
                    if bytes.fromhex(node['pubkey_ed25519']) == sn_pubkey:
                        if node['active']:
                            continue

            params = {
                "type": "solo" if contract is None else "contract",
                "pubkey_ed25519": sn_pubkey,
                "pubkey_bls": pk_bls,
                "sig_ed25519": sig_ed,
                "sig_bls": sig_bls,
                "operator": op,
                "timestamp": timestamp,
            }

            if contract is not None:
                params["contract"] = contract

            reg_array.append(params)

    result = json_response({'registrations': reg_array})
    return result


def check_stakes(stakes, total, stakers, max_stakers):
    if len(stakers) != len(stakes):
        raise ValueError(f"s and S have different lengths")
    if len(stakers) < 1:
        raise ValueError(f"at least one s/S value pair is required")
    if len(stakers) > max_stakers:
        raise ValueError(f"too many stakers ({len(stakers)} > {max_stakers})")
    if sum(stakes) > total:
        raise ValueError(f"total stake is too large ({sum(stakes)} > total)")
    if len(set(stakers)) != len(stakers):
        raise ValueError(f"duplicate staking addresses in staker list")

    remaining_stake = total
    remaining_spots = max_stakers

    for i in range(len(stakes)):
        reqd = remaining_stake // (4 if i == 0 else remaining_spots)
        if stakes[i] < reqd:
            raise ValueError(
                "reserved stake [i] ({stakers[i]}) is too low ({stakes[i]} < {reqd})"
            )
        remaining_stake -= stakes[i]
        remaining_spots -= 1


def format_currency(units: int, decimal: int = 9):
    """
    Formats an atomic currency unit to `decimal` decimal places.  The conversion is lossless (i.e.
    it does not use floating point math or involve any truncation or rounding
    """
    base = 10**decimal
    print(f"units: {units}, base: {base}, decimal: {decimal}, {units//base}")
    frac = units % base
    frac = "" if frac == 0 else f".{frac:0{decimal}d}".rstrip("0")
    return f"{units // base}{frac}"


def parse_currency(k, val: str, decimal: int = 9):
    """
    Losslessly parses a currency value such as 1.23 into an atomic integer value such as 1000000023.
    """
    pieces = val.split(".")
    if len(pieces) > 2 or not all(re.fullmatch(r"\d+", p) for p in pieces):
        raise ParseError(k, "Invalid currency amount")
    whole = int(pieces[0])
    if len(pieces) > 1:
        frac = pieces[1]
        if len(frac) > decimal:
            frac = frac[0:decimal]
        elif len(frac) < decimal:
            frac = frac.ljust(decimal, "0")
        frac = int(frac)
    else:
        frac = 0

    return whole * 10**decimal + frac


def error_response(code, **err):
    """
    Error codes that can be returned to a client when validating registration details.  The `code`
    is a short string that uniquely defines the error; some errors have extra parameters (passed
    into the `err` kwargs).  This method formats the error, then returns a dict such as:

        { "code": "short_code", "error": "English string", **err }

    This is returned, typically as an "error" key, by various endpoints.

    As a special value, if a `detail` key is present in err then the usual error will have ":
    {detail}" appended to it (the detail will also be passed along separately).
    """

    err["code"] = code
    match code:
        case "bad_request":
            msg = "Invalid request parameters"
        case "invalid_op_addr":
            msg = "Invalid operator address"
        case "invalid_op_stake":
            msg = "Invalid/unparseable operator stake"
        case "wrong_op_stake":
            # For a solo node that doesn't contribute the exact requirement
            msg = f"Invalid operator stake: exactly {format_currency(err['required'])} {TOKEN_NAME} is required for a solo node"
        case "insufficient_op_stake":
            msg = f"Insufficient operator stake: at least {format_currency(err['minimum'])} ({err['minimum'] / MAX_STAKE * 100}%) is required"
        case "invalid_contract_addr":
            msg = "Invalid contract address"
        case "invalid_res_addr":
            msg = f"Invalid reserved contributor address {err['index']}: {err['address']}"
        case "invalid_res_stake":
            msg = f"Invalid/unparseable reserved contributor amount for contributor {err['index']} ({err['address']})"
        case "insufficient_res_stake":
            msg = f"Insufficient reserved contributor stake: contributor {err['index']} ({err['address']}) must contribute at least {format_currency(err['minimum'])}"
        case "too_much":
            # for multi-contributor (solo node would get wrong_op_stake instead)
            msg = f"Total node reserved contributions are too large: {format_currency(err['total'])} exceeds the maximum stake {format_currency(err['maximum'])}"
        case "too_many":
            msg = f"Too many reserved contributors: only {err['max_contributors']} contributor spots are possible"
        case "invalid_fee":
            msg = "Invalid fee"
        case "signature":
            msg = "Invalid service node registration pubkeys/signatures"
        case _:
            msg = None

    err["error"] = f"{msg}: {err['detail']}" if "detail" in err else msg

    return json_response({"error": err})


@app.route("/validate")
def validate_registration():
    """
    Validates a registration including fee, stakes, and reserved spot requirements.  This does not
    use stored registration info at all; all information has to be submitted as part of the request.
    The data is not stored.

    Parameters for both types of stakes:
    - "pubkey_ed25519"
    - "pubkey_bls"
    - "sig_ed25519"
    - "sig_bls"
    The above are as provided by oxend for the registration.  Can be hex or base64.

    - "operator" -- the operator wallet address
    - "stake" -- the amount the operator will stake.  For a solo stake, this must be exactly equal
      to the staking requirement, but for a multi-contribution node it can be less.

    For a multi-contribution node the following must additionally be passed:
    - "contract" -- the ETH address of the multi-contribution staking contract for this node.
    - "reserved" -- optional list of reserved contributor wallets.
    - "res_stake" -- list of reserved contributor stakes.  This must be the same length and order as
      `"reserved"`.

    Various checks are performed to look for registration errors; if no errors are found then the
    result contains the key "success": true.  Otherwise the key "error" will be set to an error dict
    indicating the error that was detected.  See `error_response` for details.
    """

    stakers = []
    stakes = []

    try:
        params = parse_query_params(
            {
                "pubkey_ed25519": byte_decoder(32),
                "pubkey_bls": byte_decoder(64),
                "sig_ed25519": byte_decoder(64),
                "sig_bls": byte_decoder(128),
                "-contract": raw_eth_addr,
                "operator": raw_eth_addr,
                "stake": parse_currency,
                "-res_addr[]": None,
                "-res_stake[]": None,
                "-fee": None,
            }
        )
    except (ParseMissingError, ParseUnknownError, ParseMultipleError) as e:
        return error_response("bad_request", field=e.field, detail=str(e))
    except ParseError as e:
        code = None
        match e.field:
            case f if f.startswith("pubkey_") or f.startswith("sig_"):
                return error_response("signature", field=f, detail=str(e))
            case "operator":
                return error_response("invalid_op_addr", detail=str(e))
            case "stake":
                return error_response("invalid_op_stake")
            case "contract":
                return error_response("invalid_contract_addr")
            case f:
                return error_response("bad_request", field=f, detail=str(e))

    try:
        check_reg_keys_sigs(params)
    except SNSignatureValidationError as e:
        return error_response("signature", detail=str(e))

    solo = "contract" not in params

    for k in ("addr", "stake"):
        params.setdefault(f"res_{k}", [])

    if solo and params["res_addr"]:
        return error_response(
            "invalid_contract_addr",
            detail="the contract address is required for multi-contributor registrations",
        )

    if solo and "fee" in params:
        return error_response(
            "invalid_fee", detail="fee is not applicable to a solo node registration"
        )
    elif "fee" not in params:
        return error_response(
            "invalid_fee",
            detail="fee is required for a multi-contribution registration",
        )
    else:
        fee = params["fee"]
        fee = int(fee) if re.fullmatch(r"\d+", fee) else -1
        if not 0 <= fee <= 10000:
            return error_response(
                "invalid_fee",
                detail="fee must be an integer between 0 and 10000 (= 100.00%)",
            )

    if len(params["res_addr"]) != len(params["res_stake"]):
        return error_response(
            "bad_request",
            field="res_addr",
            detail="mismatched reserved address/stake lists",
        )

    reserved = []
    for i, (addr, stake) in enumerate(zip(params["res_addr"], params["res_stake"])):
        try:
            eth = raw_eth_addr("res_addr", addr)
        except ValueError:
            return error_response("invalid_res_addr", address=eth_format(addr), index=i+1)
        try:
            amt = parse_currency("res_stake", stake)
        except ValueError:
            return error_response(
                "invalid_res_stake", address=eth_format(addr), index=i+1
            )

        reserved.append((eth, amt))

    total_reserved = params["stake"] + sum(stake for _, stake in reserved)
    if solo:
        if total_reserved != MAX_STAKE:
            return error_response(
                "wrong_op_stake", stake=total_reserved, required=MAX_STAKE
            )
    else:
        if params["stake"] < MIN_OP_STAKE:
            return error_response(
                "insufficient_op_stake", stake=params["stake"], minimum=MIN_OP_STAKE
            )
        if total_reserved > MAX_STAKE:
            return error_response("too_much", total=total_reserved, maximum=MAX_STAKE)
        if 1 + len(reserved) > MAX_STAKERS:
            return error_response("too_many", max_contributors=MAX_STAKERS - 1)

        remaining_stake = MAX_STAKE - params["stake"]
        remaining_spots = MAX_STAKERS - 1

        for i, (addr, amt) in enumerate(reserved):
            # integer math ceiling:
            min_contr = (remaining_stake + remaining_spots - 1) // remaining_spots
            if amt < min_contr:
                return error_response(
                    "insufficient_res_stake",
                    index=i+1,
                    address=eth_format(addr),
                    minimum=min_contr,
                )
            remaining_stake -= amt
            remaining_spots -= 1

    res = {"success": True}

    if not solo:
        res["remaining_contribution"] = remaining_stake
        res["remaining_spots"] = remaining_spots
        res["remaining_min_contribution"] = (
            remaining_stake + remaining_spots - 1
        ) // remaining_spots

    return json_response(res)
