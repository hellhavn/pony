from __future__ import print_function
from pony.py23compat import ExitStack

import os, os.path, sys
from datetime import datetime
from glob import glob
from contextlib import contextmanager

from docopt import docopt

import pony
from pony import orm

from . import writer, get_cmd_exitstack, get_migration_dir
from .exceptions import MergeAborted
from .migration import Migration, MigrationLoader
from .questioner import InteractiveMigrationQuestioner

CLI_DOC = '''

Pony migration tool.

Usage:
    %(cli)s [--verbose | -v] [--fake]
    %(cli)s [--verbose | -v] [--empty --custom] make [<name>]
    %(cli)s [--verbose | -v] [--fake --dry] apply [[<start>] <end>]
    %(cli)s sql <name>
    %(cli)s list

Subcommands:
    make          Generate the migration file
    apply         Apply all generated migrations
    merge         Merge conflicts
    list          List all migrations
    sql           View sql for a given migration

Options:
    --empty       Generate a template for data migration
    --fake        Consider migrations applied
    --dry         Just print sql instead without executing it
    -v --verbose  Set sql_debug(True)
    -h --help     Show this screen
'''

cmd_exitstack = get_cmd_exitstack()


class drop_into_debugger(object):
    def __enter__(self):
        pass
    def __exit__(self, e, m, tb):
        if not e:
            return
        try:
            import ipdb as pdb
        except ImportError:
            import pdb
        print(m.__repr__(), file=sys.stderr)
        pdb.post_mortem(tb)


@contextmanager
def use_argv(args):
    sys_argv = sys.argv
    sys.argv = ['cli'] + args.split()
    yield
    sys.argv = sys_argv


def cli(db, argv=None):
    with ExitStack() as stack:
        if argv:
            stack.enter_context(use_argv(argv))
        doc = CLI_DOC % {'cli': 'cli migrate'}
        opts = docopt(doc)
        if opts.get('migrate'):
            migrate(db, opts)
            return
        raise NotImplementedError

def migrate(db, opts):
    debug = os.environ.get('PONY_DEBUG')
    verbose = opts['--verbose'] or opts.get('-v')
    fake = opts['--fake']

    for cmd in ('make', 'apply', 'sql', 'list'):
        if opts.get(cmd):
            break
    else:
        cmd = None
    if verbose:
        orm.sql_debug(True)

    if debug:
        cmd_exitstack.enter_context(drop_into_debugger())
    with cmd_exitstack:
        if cmd == 'make':
            Migration.make(db=db, empty=opts['--empty'], custom=opts['--custom'],
                           filename=opts['<name>'])
            return
        if cmd == 'list':
            show_migrations(db=db)
            return
        if cmd == 'apply':
            if opts['<start>'] and not opts['<end>']:
                # https://github.com/docopt/docopt/issues/358
                kw = {
                    'name_end': find_migration(opts['<start>']),
                    'name_start': None,
                }
            elif opts['<start>'] and opts['<end>']:
                kw = {
                    'name_start': find_migration(opts['<start>']),
                    'name_end': find_migration(opts['<end>']),
                }
            else:
                kw = {}
            Migration.apply(db=db, is_fake=fake, dry_run=opts['--dry'], **kw)
            return

        if cmd == 'sql':
            name = find_migration(opts['<name>'])
            Migration.apply(db=db, dry_run=True, name_exact=name)
            return
        raise NotImplementedError

def find_migration(name):
    p = os.path.join(get_migration_dir(), name)
    files = glob('{}*'.format(p))
    if len(files) > 1:
        files = ', '.join(files)
        raise Exception('Multiple files found: {}'.format(files))
    elif not files:
        raise Exception('No files for {}'.format(name))
    p = files[0]
    p = os.path.basename(p)
    assert p[-3:] == '.py'
    return p[:-3]

@orm.db_session
def show_migrations(db, fail_fast=False):
    '''
    List the migration dir.
    if migration name is specified, print its sql.
    '''
    cmd_exitstack.callback(db.disconnect)
    Migration.make_entity(db)
    db.schema = db.generate_schema()
    loader = MigrationLoader()
    leaves = loader.graph.leaf_nodes()
    if not leaves:
        print('No migrations')
        return
    if len(leaves) > 1 and not fail_fast:
        # Merge required
        questioner = InteractiveMigrationQuestioner()
        if questioner.ask_merge(leaves):
            # not tested?
            merge(loader=loader, leaves=leaves)
            show_migrations(fail_fast=True)
            return
        return
    leaf = leaves[0]
    names = loader.graph.forwards_plan(leaf)

    try:
        with orm.db_session:
            orm.exists(m for m in db.Migration)
    except orm.core.DatabaseError as ex:
        print('No Migration table. Please apply the initial migration.')
        return

    saved = orm.select((m.name, m.applied) for m in db.Migration if m.name in names) \
            .order_by(lambda: m.applied)[:]
    if saved:
        saved, _ = zip(*saved)
    for name in saved:
        print('+ {}'.format(name))
    for name in names:
        if name in saved:
            continue
        print('  {}'.format(name))

def merge(db=None, loader=None, leaves=None):
    if loader is None:
        loader = MigrationLoader()
        loader.build_graph()
        leaves = loader.graph.leaf_nodes()
    if len(leaves) <= 1:
        print('Nothing to merge.')
        return

    questioner = InteractiveMigrationQuestioner()
    if not questioner.ask_merge(leaves):
        raise MergeAborted

    cmd_exitstack.callback(db.disconnect)
    name = Migration._generate_name(loader)
    ctx = {
        'deps': leaves,
        'version': pony.__version__,
        'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M"),
        'body': 'operations = []',
        'imports': '',
    }
    generated = writer.MIGRATION_TEMPLATE.format(**ctx)
    migrations = get_migration_dir()
    p = os.path.join(migrations, '{}.py'.format(name))
    with open(p, 'w') as f:
        f.write(generated)


def add_migrate_to_click(click_group, db, name='migrate'):
    '''
    Compatibility function for click (click.pocoo.org).

    For flask app:
        add_migrate_to_click(app.cli, db)
    '''
    import click

    @click.command(name, context_settings={'ignore_unknown_options': True})
    @click.argument('_arg', nargs=-1, type=click.UNPROCESSED)
    def do_migrate(_arg):
        cli(db)

    click_group.add_command(do_migrate)