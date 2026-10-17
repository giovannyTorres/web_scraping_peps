"""Offline local-file diagnostics, including packaged dependency execution."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps import cli


HTML = '''<html><head><title>Informe público del archivo documental local</title></head>
<body><div class="main right-sidebar"><h1>Informe público del archivo documental local</h1>
<article><div class="entry-content"><p>El archivo local describe una revisión técnica de documentos ficticios.
El equipo conserva la procedencia de cada página y explica las limitaciones de su lectura.
PRIMER_MARCADOR permite comprobar que llegó el primer párrafo completo al resultado.</p>
<p>La segunda parte documenta los controles de acceso, el avance observado y las tareas pendientes.
Cada conclusión debe apoyarse en un texto conservado y no en un encabezado aislado.
SEGUNDO_MARCADOR confirma la recuperación del segundo párrafo del mismo documento.</p>
</div></article></div></body></html>'''


class LocalHtmlCliTests(unittest.TestCase):
    def test_actual_extraction_opens_no_bank_and_attempts_no_network(self):
        with tempfile.TemporaryDirectory() as staging:
            root = Path(staging)
            document = root / 'document.html'
            document.write_text(HTML, encoding='utf-8')
            output = io.StringIO()
            with patch('sys.argv', ['webscope', '--data-dir', str(root / 'bank'), 'extract-html', str(document)]), \
                    patch.object(cli, 'Engine', side_effect=AssertionError('Unexpected bank initialization')), \
                    patch('socket.socket.connect', side_effect=AssertionError('Unexpected TCP connection')), \
                    patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected DNS lookup')), \
                    redirect_stdout(output):
                cli.main()
            result = json.loads(output.getvalue())
            self.assertIn('PRIMER_MARCADOR', result['text'])
            self.assertIn('SEGUNDO_MARCADOR', result['text'])
            self.assertTrue(result['runtime']['available'])
            self.assertFalse(result['network'])
            self.assertFalse(result['bank_opened'])
            self.assertFalse((root / 'bank').exists())

    def test_oversized_invalid_encoding_and_nonfiles_fail_before_bank_creation(self):
        with tempfile.TemporaryDirectory() as staging:
            root = Path(staging)
            large = root / 'large.html'
            large.write_bytes(b' ' * 2_000_001)
            invalid = root / 'encoding.html'
            invalid.write_bytes(b'\xff')
            for target in (large, invalid, root):
                with self.subTest(target=target), \
                        patch('sys.argv', ['webscope', '--data-dir', str(root / 'bank'), 'extract-html', str(target)]), \
                        patch.object(cli, 'Engine', side_effect=AssertionError('Unexpected bank initialization')), \
                        redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    cli.main()
                self.assertEqual(stopped.exception.code, 2)
                self.assertFalse((root / 'bank').exists())

    def test_unknown_encoding_is_a_clean_cli_error_without_bank_or_network(self):
        with tempfile.TemporaryDirectory() as staging:
            root = Path(staging)
            document = root / 'document.html'
            document.write_text(HTML, encoding='utf-8')
            error = io.StringIO()
            with patch('sys.argv', ['webscope', '--data-dir', str(root / 'bank'), 'extract-html',
                                    str(document), '--encoding', 'unknown-codec']), \
                    patch.object(cli, 'Engine', side_effect=AssertionError('Unexpected bank initialization')), \
                    patch('socket.socket.connect', side_effect=AssertionError('Unexpected TCP connection')), \
                    patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected DNS lookup')), \
                    redirect_stderr(error), self.assertRaises(SystemExit) as stopped:
                cli.main()
            self.assertEqual(stopped.exception.code, 2)
            self.assertNotIn('Traceback', error.getvalue())
            self.assertFalse((root / 'bank').exists())


if __name__ == '__main__':
    unittest.main()
