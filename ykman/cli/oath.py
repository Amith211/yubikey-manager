# Copyright (c) 2015 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import click
import logging
from .util import (
    click_force_option,
    click_postpone_execution,
    click_callback,
    click_parse_b32_key,
    click_prompt,
    ykman_group,
    prompt_for_touch,
    prompt_timeout,
    EnumChoice,
)
from yubikit.core import USB_INTERFACE
from yubikit.core.smartcard import ApduError, SW
from yubikit.oath import (
    OathSession,
    CredentialData,
    OATH_TYPE,
    HASH_ALGORITHM,
    parse_b32_key,
)
from ..oath import is_steam, calculate_steam, is_hidden
from ..device import is_fips_version
from ..settings import Settings


logger = logging.getLogger(__name__)


@ykman_group(USB_INTERFACE.CCID)
@click.pass_context
@click_postpone_execution
def oath(ctx):
    """
    Manage the OATH Application.

    Examples:

    \b
      Generate codes for credentials starting with 'yubi':
      $ ykman oath accounts code yubi

    \b
      Add a touch credential with the secret key f5up4ub3dw and the name yubico:
      $ ykman oath accounts add yubico f5up4ub3dw --touch

    \b
      Set a password for the OATH application:
      $ ykman oath access change-password
    """
    try:
        session = OathSession(ctx.obj["conn"])
        ctx.obj["session"] = session
        ctx.obj["settings"] = Settings("oath")
    except ApduError as e:
        if e.sw == SW.FILE_NOT_FOUND:
            ctx.fail("The OATH application can't be found on this YubiKey.")
        raise


@oath.command()
@click.pass_context
def info(ctx):
    """
    Display status of OATH application.
    """
    session = ctx.obj["session"]
    version = session.version
    click.echo("OATH version: {}.{}.{}".format(version[0], version[1], version[2]))
    click.echo("Password protection: " + ("enabled" if session.locked else "disabled"))

    keys = ctx.obj["settings"].get("keys", {})
    if session.locked and session.device_id in keys:
        click.echo("The password for this YubiKey is remembered by ykman.")

    if is_fips_version(version):
        click.echo("FIPS Approved Mode: {}".format("Yes" if session.locked else "No"))


@oath.command()
@click.pass_context
@click.confirmation_option(
    "-f",
    "--force",
    prompt="WARNING! This will delete "
    "all stored OATH credentials and restore factory settings?",
)
def reset(ctx):
    """
    Reset all OATH data.

    This action will wipe all credentials and reset factory settings for
    the OATH application on the YubiKey.
    """

    session = ctx.obj["session"]
    click.echo("Resetting OATH data...")
    old_id = session.device_id
    session.reset()

    settings = ctx.obj["settings"]
    keys = settings.setdefault("keys", {})
    if old_id in keys:
        del keys[old_id]
        settings.write()

    click.echo("Success! All OATH credentials have been cleared from the YubiKey.")


click_password_option = click.option(
    "-p", "--password", help="Provide a password to unlock the YubiKey."
)


def _validate(ctx, key, remember):
    try:
        session = ctx.obj["session"]
        session.validate(key)
        if remember:
            settings = ctx.obj["settings"]
            keys = settings.setdefault("keys", {})
            keys[session.device_id] = key.hex()
            settings.write()
            click.echo("Password remembered.")
    except Exception:
        ctx.fail("Authentication to the YubiKey failed. Wrong password?")


def _init_session(ctx, password, remember, prompt="Enter the password"):
    session = ctx.obj["session"]
    settings = ctx.obj["settings"]
    keys = settings.setdefault("keys", {})
    device_id = session.device_id

    if session.locked:
        if password:  # If password argument given, use it
            key = session.derive_key(password)
        elif device_id in keys:  # If remembered, use key
            key = bytes.fromhex(keys[device_id])
        else:  # Prompt for password
            password = click_prompt(prompt, hide_input=True)
            key = session.derive_key(password)
        _validate(ctx, key, remember)
    elif password:
        ctx.fail("Password provided, but no password is set.")


@oath.group()
def access():
    """Manage password protection for OATH."""


@access.command()
@click.pass_context
@click_password_option
@click.option(
    "-c", "--clear", is_flag=True, help="Clear the current password.",
)
@click.option("-n", "--new-password", help="Provide a new password as an argument.")
def change(ctx, password, clear, new_password):
    """
    Change the password used to protect OATH credentials.

    Allows you to set or change a password that will be required to access the OATH
    credentials stored on the YubiKey.
    """
    if clear and new_password:
        ctx.fail("--clear cannot be combined with --new-password.")

    _init_session(ctx, password, False, prompt="Enter the current password")

    session = ctx.obj["session"]
    settings = ctx.obj["settings"]
    keys = settings.setdefault("keys", {})
    device_id = session.device_id

    if clear:
        session.unset_key()
        if device_id in keys:
            del keys[device_id]
            settings.write()

        click.echo("Password cleared from YubiKey.")
    else:
        if not new_password:
            new_password = click_prompt(
                "Enter the new password", hide_input=True, confirmation_prompt=True
            )
        key = session.derive_key(new_password)
        session.set_key(key)
        click.echo("Password updated.")
        if device_id in keys:
            keys[device_id] = key.hex()
            settings.write()
            click.echo("Password remembered.")


@access.command()
@click.pass_context
@click_password_option
def remember(ctx, password):
    """
    Store the YubiKeys password on this computer to avoid having to enter it
    on each use.
    """
    session = ctx.obj["session"]
    device_id = session.device_id
    settings = ctx.obj["settings"]
    keys = settings.setdefault("keys", {})

    if not session.locked:
        if device_id in keys:
            del keys[session.device_id]
            settings.write()
        click.echo("This YubiKey is not password protected.")
    else:
        if not password:
            password = click_prompt("Enter the password", hide_input=True)
        key = session.derive_key(password)
        _validate(ctx, key, True)


def _clear_all_passwords(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return

    settings = Settings("oath")
    if "keys" in settings:
        del settings["keys"]
        settings.write()
    click.echo("All passwords have been forgotten.")
    ctx.exit()


@access.command()
@click.pass_context
@click.option(
    "-a",
    "--all",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_clear_all_passwords,
    help="Remove all stored passwords.",
)
def forget(ctx):
    """
    Remove a stored password from this computer.
    """
    session = ctx.obj["session"]
    device_id = session.device_id
    settings = ctx.obj["settings"]
    keys = settings.setdefault("keys", {})

    if device_id in keys:
        del keys[session.device_id]
        settings.write()
        click.echo("Password forgotten.")
    else:
        click.echo("No password stored for this YubiKey.")


click_remember_option = click.option(
    "-r", "--remember", is_flag=True, help="Remember the password on this machine.",
)

click_touch_option = click.option(
    "-t", "--touch", is_flag=True, help="Require touch on YubiKey to generate code."
)


click_show_hidden_option = click.option(
    "-H", "--show-hidden", is_flag=True, help="Include hidden credentials."
)


def _string_id(credential):
    return credential.id.decode("utf-8")


def _error_multiple_hits(ctx, hits):
    click.echo(
        "Error: Multiple matches, please make the query more specific.", err=True
    )
    click.echo("", err=True)
    for cred in hits:
        click.echo(_string_id(cred), err=True)
    ctx.exit(1)


def _search(creds, query, show_hidden):
    hits = []
    for c in creds:
        cred_id = _string_id(c)
        if not show_hidden and is_hidden(c):
            continue
        if cred_id == query:
            return [c]
        if query.lower() in cred_id.lower():
            hits.append(c)
    return hits


@oath.group()
def accounts():
    """Manage and use OATH accounts."""


@accounts.command()
@click.argument("name")
@click.argument("secret", callback=click_parse_b32_key, required=False)
@click.option(
    "-o",
    "--oath-type",
    type=EnumChoice(OATH_TYPE),
    default=OATH_TYPE.TOTP.name,
    help="Time-based (TOTP) or counter-based (HOTP) credential.",
    show_default=True,
)
@click.option(
    "-d",
    "--digits",
    type=click.Choice(["6", "7", "8"]),
    default="6",
    help="Number of digits in generated code.",
    show_default=True,
)
@click.option(
    "-a",
    "--algorithm",
    type=EnumChoice(HASH_ALGORITHM),
    default=HASH_ALGORITHM.SHA1.name,
    show_default=True,
    help="Algorithm to use for code generation.",
)
@click.option(
    "-c",
    "--counter",
    type=click.INT,
    default=0,
    help="Initial counter value for HOTP credentials.",
)
@click.option("-i", "--issuer", help="Issuer of the credential.")
@click.option(
    "-p",
    "--period",
    help="Number of seconds a TOTP code is valid.",
    default=30,
    show_default=True,
)
@click_touch_option
@click_force_option
@click_password_option
@click_remember_option
@click.pass_context
def add(
    ctx,
    secret,
    name,
    issuer,
    period,
    oath_type,
    digits,
    touch,
    algorithm,
    counter,
    force,
    password,
    remember,
):
    """
    Add a new credential.

    This will add a new credential to the YubiKey.
    """

    digits = int(digits)

    if not secret:
        while True:
            secret = click_prompt("Enter a secret key (base32)")
            try:
                secret = parse_b32_key(secret)
                break
            except Exception as e:
                click.echo(e)

    _init_session(ctx, password, remember)

    _add_cred(
        ctx,
        CredentialData(
            name, oath_type, algorithm, secret, digits, period, counter, issuer
        ),
        touch,
        force,
    )


@click_callback()
def click_parse_uri(ctx, param, val):
    try:
        return CredentialData.parse_uri(val)
    except ValueError:
        raise click.BadParameter("URI seems to have the wrong format.")


@accounts.command()
@click.argument("data", callback=click_parse_uri, required=False, metavar="URI")
@click_touch_option
@click_force_option
@click_password_option
@click_remember_option
@click.pass_context
def uri(ctx, data, touch, force, password, remember):
    """
    Add a new credential from an otpauth:// URI.

    Use a URI to add a new credential to the YubiKey.
    """

    if not data:
        while True:
            uri = click_prompt("Enter an OATH URI")
            try:
                data = CredentialData.parse_uri(uri)
                break
            except Exception as e:
                click.echo(e)

    # Steam is a special case where we allow the otpauth
    # URI to contain a 'digits' value of '5'.
    if data.digits == 5 and is_steam(data):
        data.digits = 6

    _init_session(ctx, password, remember)
    _add_cred(ctx, data, touch, force)


def _add_cred(ctx, data, touch, force):
    session = ctx.obj["session"]
    version = session.version

    if not (0 < len(data.name) <= 64):
        ctx.fail("Name must be between 1 and 64 bytes.")

    if len(data.secret) < 2:
        ctx.fail("Secret must be at least 2 bytes.")

    if touch and version < (4, 2, 6):
        ctx.fail("Touch-required credentials not supported on this key.")

    if data.counter and data.oath_type != OATH_TYPE.HOTP:
        ctx.fail("Counter only supported for HOTP credentials.")

    if data.hash_algorithm == HASH_ALGORITHM.SHA512 and (
        version < (4, 3, 1) or is_fips_version(version)
    ):
        ctx.fail("Algorithm SHA512 not supported on this YubiKey.")

    creds = session.list_credentials()
    cred_id = data.get_id()
    if not force and any(cred.id == cred_id for cred in creds):
        click.confirm(
            "A credential called {} already exists on this YubiKey."
            " Do you want to overwrite it?".format(data.name),
            abort=True,
            err=True,
        )

    firmware_overwrite_issue = (4, 0, 0) < version < (4, 3, 5)
    cred_is_subset = any(
        (cred.id.startswith(cred_id) and cred.id != cred_id) for cred in creds
    )

    #  YK4 has an issue with credential overwrite in firmware versions < 4.3.5
    if firmware_overwrite_issue and cred_is_subset:
        ctx.fail("Choose a name that is not a subset of an existing credential.")

    try:
        session.put_credential(data, touch)
    except ApduError as e:
        if e.sw == SW.NO_SPACE:
            ctx.fail("No space left on the YubiKey for OATH credentials.")
        elif e.sw == SW.COMMAND_ABORTED:
            # Some NEOs do not use the NO_SPACE error.
            ctx.fail("The command failed. Is there enough space on the YubiKey?")
        else:
            raise


@accounts.command()
@click_show_hidden_option
@click.pass_context
@click.option("-o", "--oath-type", is_flag=True, help="Display the OATH type.")
@click.option("-p", "--period", is_flag=True, help="Display the period.")
@click_password_option
@click_remember_option
def list(ctx, show_hidden, oath_type, period, password, remember):
    """
    List all credentials.

    List all credentials stored on the YubiKey.
    """
    _init_session(ctx, password, remember)
    session = ctx.obj["session"]
    creds = [
        cred
        for cred in session.list_credentials()
        if show_hidden or not is_hidden(cred)
    ]
    creds.sort()
    for cred in creds:
        click.echo(_string_id(cred), nl=False)
        if oath_type:
            click.echo(u", {}".format(cred.oath_type.name), nl=False)
        if period:
            click.echo(", {}".format(cred.period), nl=False)
        click.echo()


@accounts.command()
@click_show_hidden_option
@click.pass_context
@click.argument("query", required=False, default="")
@click.option(
    "-s",
    "--single",
    is_flag=True,
    help="Ensure only a single match, and output only the code.",
)
@click_password_option
@click_remember_option
def code(ctx, show_hidden, query, single, password, remember):
    """
    Generate codes.

    Generate codes from credentials stored on the YubiKey.
    Provide a query string to match one or more specific credentials.
    Touch and HOTP credentials require a single match to be triggered.
    """

    _init_session(ctx, password, remember)

    session = ctx.obj["session"]
    entries = session.calculate_all()
    creds = _search(entries.keys(), query, show_hidden)

    if len(creds) == 1:
        cred = creds[0]
        code = entries[cred]
        if cred.touch_required:
            prompt_for_touch()
        try:
            if cred.oath_type == OATH_TYPE.HOTP:
                with prompt_timeout():
                    # HOTP might require touch, we don't know.
                    # Assume yes after 500ms.
                    code = session.calculate_code(cred)
            elif code is None:
                code = session.calculate_code(cred)
        except ApduError as e:
            if e.sw == SW.SECURITY_CONDITION_NOT_SATISFIED:
                ctx.fail("Touch credential timed out!")
        entries[cred] = code

    elif single and len(creds) > 1:
        _error_multiple_hits(ctx, creds)

    elif single and len(creds) == 0:
        ctx.fail("No matching credential found.")

    if single and creds:
        if is_steam(cred):
            click.echo(calculate_steam(session, cred))
        else:
            click.echo(code.value)
    else:
        outputs = []
        for cred in sorted(creds):
            code = entries[cred]
            if code:
                code = code.value
            elif cred.touch_required:
                code = "[Touch Credential]"
            elif cred.oath_type == OATH_TYPE.HOTP:
                code = "[HOTP Credential]"
            else:
                code = ""
            if is_steam(cred):
                code = calculate_steam(session, cred)
            outputs.append((_string_id(cred), code))

        longest_name = max(len(n) for (n, c) in outputs) if outputs else 0
        longest_code = max(len(c) for (n, c) in outputs) if outputs else 0
        format_str = u"{:<%d}  {:>%d}" % (longest_name, longest_code)

        for name, result in outputs:
            click.echo(format_str.format(name, result))


@accounts.command()
@click.pass_context
@click.argument("query")
@click.option("-f", "--force", is_flag=True, help="Confirm deletion without prompting")
@click_password_option
@click_remember_option
def delete(ctx, query, force, password, remember):
    """
    Delete a credential.

    Delete a credential from the YubiKey.
    Provide a query string to match the credential to delete.
    """

    _init_session(ctx, password, remember)
    session = ctx.obj["session"]
    creds = session.list_credentials()
    hits = _search(creds, query, True)
    if len(hits) == 0:
        click.echo("No matches, nothing to be done.")
    elif len(hits) == 1:
        cred = hits[0]
        if force or (
            click.confirm(
                u"Delete credential: {} ?".format(_string_id(cred)),
                default=False,
                err=True,
            )
        ):
            session.delete_credential(cred.id)
            click.echo(u"Deleted {}.".format(_string_id(cred)))
        else:
            click.echo("Deletion aborted by user.")

    else:
        _error_multiple_hits(ctx, hits)
