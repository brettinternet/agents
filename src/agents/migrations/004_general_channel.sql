UPDATE conversations SET address='#general', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE address='#all-hands';
