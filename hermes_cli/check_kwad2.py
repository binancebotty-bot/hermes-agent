import sys
sys.path.insert(0, '.')
ns = {}
exec(open('tests/hermes_cli/test_kanban_worker_admission_wait.py').read(), ns)
print("kanban_worker_client in ns:", 'kanban_worker_client' in ns)
if 'kanban_worker_client' in ns:
    print("kanban_worker_client:", ns['kanban_worker_client'])
