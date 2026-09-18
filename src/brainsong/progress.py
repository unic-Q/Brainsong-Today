"""Small, flushed operational log. Never accepts API payloads or credentials."""
from datetime import datetime
from time import monotonic


class Progress:
    def __init__(self, path):
        self.path = path
        self.started = monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, stage, **counts):
        # Stages are developer/config identifiers, not article text or responses.
        stage = str(stage).replace('\n', ' ').replace('\r', ' ')[:100]
        fields = ' '.join(f'{key}={value}' for key, value in counts.items()
                          if isinstance(value, (int, float, bool)))
        line = f'{datetime.now().isoformat(timespec="seconds")} +{monotonic()-self.started:.1f}s {stage} {fields}'
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(line + '\n')
        print(line, flush=True)
