import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from column_mapping import default_mappings, mapped_cells, validate_mappings
import drive_batch_uploader as upload
import paste_link_download_page as paste
import sheets_batch_downloader as sheets
import updater


class CustomWorkflows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_mapped_upload_custom_headers_values_and_columns(self):
        mapping = [dict(header='素材地址', column='AA', source='file_url', value=''),
                   dict(header='项目名称', column='C', source='custom', value='新项目'),
                   dict(header='跳过字段', column='', source='date', value=''),
                   dict(header='文件名', column='D', source='file_name', value='')]
        worker = upload.UploadWorker('', '', 'sheet', 'parent', '入库', '日志', [], True, True,
                                     inbound_mappings=mapping, data_row=3)
        client = Mock()
        worker._write_inbound(client, upload.UploadTask(1, False, cat2='子类'), 'https://file', '原名.mp4')
        args = client.insert_inbound_row.call_args
        self.assertEqual(args.args[2][2:4], ['新项目', '原名.mp4'])
        self.assertEqual(args.args[2][26], 'https://file')
        self.assertEqual(args.kwargs['insert_before_row'], 3)
        self.assertEqual(mapped_cells([], {}), {})
        for col in ('0', '-1', 'A1', 'ZZZZ'):
            with self.subTest(col=col), self.assertRaises(ValueError):
                validate_mappings([dict(header='X', column=col, source='custom')])
        with self.assertRaises(ValueError):
            validate_mappings([dict(header='X', column=c, source='custom') for c in ('a', '1')])

    def test_header_sync_only_targets_configured_columns(self):
        mapping = [dict(header='新表头', column='B', source='custom', value='')]
        client = Mock(account_label='test')
        task = upload.UploadTask(1, False, files=[upload.UploadFileItem('file', 'file')])
        worker = upload.UploadWorker('', '', 'sheet', 'parent', 'data', 'log', [task], True, True,
                                     inbound_mappings=mapping, sync_headers=True, header_row=2, data_row=3)
        # Stop after the header operation, before any external upload.
        client.write_row_fields.side_effect = lambda *args: worker.stop()
        with patch.object(upload, 'GoogleClient', return_value=client):
            worker.run()
        client.write_row_fields.assert_called_once_with('sheet', 'data', 2, {'B': '新表头'})
        client.get_or_create_folder_path.assert_not_called()

    def test_category_non_adjacent_columns_and_merged_parent_reset(self):
        client = object.__new__(sheets.GoogleClient)
        client.sheets = Mock()
        values = client.sheets.spreadsheets().values()
        # Read C:H; cat1=H, cat2=C, cat3=F, type disabled.
        values.get().execute.return_value = {'values': [
            ['二级甲', '', '', '三级甲', '', '一级甲'],
            ['', '', '', '三级乙'],
            ['', '', '', '三级丙', '', '一级乙']]}
        rows = client.read_category_rows('sid', "分类'表", ['H', 'C', 'F', ''], 4)
        self.assertEqual(rows, [['一级甲', '二级甲', '三级甲', ''],
                                ['一级甲', '二级甲', '三级乙', ''],
                                ['一级乙', '', '三级丙', '']])
        self.assertEqual(values.get.call_args.kwargs['range'], "'分类''表'!C4:H")
        self.assertEqual(client.read_category_rows('sid', 's', ['', '', '', '']), [])
        rows = client.read_category_rows('sid', 's', ['H', 'C', 'F', ''], inherit=False)
        self.assertEqual(rows[1][:2], ['', ''])

    def page(self):
        with patch.object(upload, 'settings_path', return_value=str(self.root / 'settings.json')), patch.object(
                upload, 'task_queue_path', return_value=str(self.root / 'queue.json')):
            return upload.DriveBatchUploadPage(token_path=str(self.root / "token.json"))

    def test_free_category_selection_and_add_task_without_first_level(self):
        page = self.page()
        page.on_categories_loaded([['A', 'B', 'C', 'T'], ['X', 'Y', 'Z', 'U']])
        page.cat3_combo.setCurrentText('Z')
        page.cat2_combo.setCurrentText('B')
        self.assertEqual(page.cat1_combo.currentText(), '')
        page.cat1_combo.setCurrentText('A')
        self.assertEqual(page.cat3_combo.currentText(), 'Z')
        page.cat1_combo.setCurrentText('')
        local = self.root / 'test.mp4'; local.write_bytes(b'file')
        page.pending_files = [str(local)]
        page.uploader_edit.setCurrentText('test')
        with patch.object(upload, 'task_queue_path', return_value=str(self.root / 'queue.json')):
            page.add_task()
        self.assertEqual((page.tasks[0].cat1, page.tasks[0].cat2, page.tasks[0].cat3), ('', 'B', 'Z'))
        page.deleteLater()

    def test_old_mapping_migration_and_new_settings_roundtrip(self):
        cfg = self.root / 'settings.json'
        cfg.write_text(json.dumps({'inbound_cols': {'file_url': 'Z'}}))
        page = self.page()
        self.assertEqual(next(m['column'] for m in page.mapping_editor.rows() if m['source'] == 'file_url'), 'Z')
        page.mapping_editor.set_rows([dict(header='自定义表头', column='C', source='custom', value='自定义值')])
        page.category_mapping.item(2, 0).setText('产品类型')
        page.category_mapping.item(2, 1).setText('K')
        with patch.object(upload, 'settings_path', return_value=str(cfg)):
            page.save_settings_and_collapse()
        restored = self.page()
        self.assertEqual(restored.mapping_editor.rows(), page.mapping_editor.rows())
        self.assertEqual(restored.category_mapping.item(2, 1).text(), 'K')
        self.assertIn('产品类型', restored.category_labels[2].text())
        restored.deleteLater(); page.deleteLater()

    def fake_client(self):
        client = Mock(account_label='test')
        client.get_drive_file_name.side_effect = lambda fid: {'rootfolder': '父级', 'singlefile': '原名'}[fid]
        client.list_folder_files.return_value = [
            {'id': 'file01', 'name': 'one.mp4', 'relative_path': '子级/深层/one.mp4'},
            {'id': 'file02', 'name': 'two.jpg', 'relative_path': '子级/two.jpg'}]
        def download(fid, target, *args, **kwargs):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(fid.encode())
            return target
        client.download_drive_file.side_effect = download
        return client

    def run_worker(self, config, urls, client):
        cfg = dict(output_dir=str(self.root), dedupe_by_id=True, **config)
        tasks = [paste.MatchedTask(paste_url=url, matched=True) for url in urls]
        worker = paste.PasteDownloadWorker('', '', cfg, tasks)
        errors = []; worker.failed.connect(errors.append)
        with patch.object(paste, 'GoogleClient', return_value=client), patch.object(paste.time, 'sleep'):
            worker.run()
        self.assertEqual(errors, [])
        self.assertTrue(all(t.status == paste.STATUS_DONE for t in tasks), [t.note for t in tasks])
        return worker

    def test_nested_folder_sequence_and_standalone_continuity(self):
        client = self.fake_client()
        self.run_worker(dict(naming_mode='sequence', start_number=5, folder_layout='preserve'),
                        ['https://drive.google.com/drive/folders/rootfolder',
                         'https://drive.google.com/file/d/singlefile/view'], client)
        self.assertTrue((self.root / '父级/子级/深层/05.mp4').is_file())
        self.assertTrue((self.root / '父级/子级/06.jpg').is_file())
        self.assertTrue((self.root / '07').is_file())
        client.download_drive_file.reset_mock()
        self.run_worker(dict(naming_mode='sequence', start_number=5, folder_layout='preserve'),
                        ['https://drive.google.com/drive/folders/rootfolder'], client)
        client.download_drive_file.assert_not_called()

    def test_flat_parent_original_names_and_default_sequence(self):
        client = self.fake_client()
        self.run_worker(dict(naming_mode='original', folder_layout='flat'),
                        ['https://drive.google.com/drive/folders/rootfolder'], client)
        self.assertTrue((self.root / '父级/one.mp4').is_file())
        self.assertTrue((self.root / '父级/two.jpg').is_file())
        self.assertFalse((self.root / '父级/子级').exists())
        worker = paste.PasteDownloadWorker('', '', {'naming_mode': 'sequence'}, [])
        self.assertEqual(Path(worker._target(str(self.root), 'file.txt')[0]).name, '01.txt')
        self.assertEqual(Path(paste._build_target_path(str(self.root), '', 'README')).name, 'README')

    def test_sequence_resume_avoids_existing_numbers(self):
        (self.root / '05.mp4').write_bytes(b'existing')
        worker = paste.PasteDownloadWorker('', '', dict(output_dir=str(self.root), naming_mode='sequence', start_number=5), [])
        self.assertEqual(Path(worker._target(str(self.root), 'name.jpg')[0]).name, '06.jpg')

    def test_http_server_filename_respects_numbering_and_extensionless_name(self):
        def response(name):
            res = io.BytesIO(b'contents')
            res.headers = {'Content-Disposition': f'attachment; filename="{name}"'}
            return res
        public = sheets.PublicDownloader()
        with patch.object(sheets, 'open_public', return_value=response('real.mp4')):
            saved = public.download('https://example.com/endpoint', str(self.root / '05'),
                                    output_root=str(self.root), sequence_number=5, preserve_original=True)
        self.assertEqual(Path(saved).name, '05.mp4')
        with patch.object(sheets, 'open_public', return_value=response('README')):
            saved = public.download('https://example.com/endpoint', str(self.root / 'endpoint'),
                                    output_root=str(self.root), preserve_original=True)
        self.assertEqual(Path(saved).name, 'README')

    def test_people_dropdown_supports_sheet_values_and_typed_names(self):
        page = self.page()
        page.uploader_edit.setCurrentText('手动姓名')
        page.on_category_headers_loaded(['业务', '产品', '系列', '格式', '制作人', '审片人'])
        page.on_categories_loaded([['A', 'B', 'C', 'T', '张三', '李四'],
                                   ['', '', '', '', '张三', '王五']])
        self.assertTrue(page.uploader_edit.isEditable())
        self.assertTrue(page.reviewer_edit.isEditable())
        self.assertEqual(page.uploader_edit.currentText(), '手动姓名')
        self.assertEqual([page.uploader_edit.itemText(i) for i in range(page.uploader_edit.count())], ['', '张三'])
        page.reviewer_edit.setCurrentText('王五')
        self.assertEqual(page.reviewer_edit.currentText(), '王五')
        self.assertEqual(page.category_mapping.item(0, 0).text(), '业务')
        self.assertIn('业务', page.category_labels[0].text())
        self.assertTrue(page.mapping_editor.table.isColumnHidden(2))
        self.assertTrue(page.mapping_editor.table.isColumnHidden(3))
        # Switching sheets clears old choices, while preserving typed selections.
        page.on_category_headers_loaded(['新表头'])
        page.on_categories_loaded([['X', '', '', '', '赵六', '']])
        self.assertEqual(page.uploader_edit.findText('张三'), -1)
        self.assertEqual(page.uploader_edit.currentText(), '手动姓名')
        self.assertEqual(page.category_mapping.item(1, 0).text(), '二级分类')
        page.deleteLater()

    def test_mapped_headers_and_people_do_not_inherit(self):
        client = object.__new__(sheets.GoogleClient)
        client.sheets = Mock()
        api = client.sheets.spreadsheets().values()
        api.get().execute.return_value = {'values': [['业务', '产品', '系列', '格式', '制作人', '审片人']]}
        self.assertEqual(client.read_mapped_headers('sid', '分类', list('ABCDEF'), 3),
                         ['业务', '产品', '系列', '格式', '制作人', '审片人'])
        self.assertEqual(api.get.call_args.kwargs['range'], "'分类'!A3:F3")
        api.get().execute.return_value = {'values': [['A', 'B', 'C', 'T', '张三', '李四'],
                                                     ['', '', 'D', '', '', '王五']]}
        rows = client.read_category_rows('sid', '分类', list('ABCDEF'), 4, True)
        self.assertEqual(rows[1], ['A', 'B', 'D', '', '', '王五'])

    def test_mapping_section_collapses_and_saved_state_restores(self):
        page = self.page()
        self.assertTrue(page.mapping_toggle.isChecked())
        page.mapping_toggle.setChecked(False)
        self.assertTrue(page.mapping_body.isHidden())
        page.mapping_toggle.setChecked(True)
        self.assertFalse(page.mapping_body.isHidden())
        with patch.object(upload, 'settings_path', return_value=str(self.root / 'settings.json')):
            page.save_settings_and_collapse()
        self.assertTrue(page.mapping_body.isHidden())
        restored = self.page()
        self.assertFalse(restored.mapping_toggle.isChecked())
        self.assertTrue(restored.mapping_body.isHidden())
        page.deleteLater(); restored.deleteLater()

    def test_wheel_requires_focus_for_combo_and_spin_controls(self):
        from PySide6.QtCore import Qt, QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtWidgets import QWidget, QVBoxLayout, QLineEdit
        from focus_widgets import FocusComboBox, FocusSpinBox
        window = QWidget()
        layout = QVBoxLayout(window)
        other = QLineEdit(); layout.addWidget(other)
        combo = FocusComboBox(); combo.addItems(['A', 'B', 'C']); combo.setCurrentIndex(1)
        editable = FocusComboBox(); editable.setEditable(True); editable.addItems(['张三', '李四', '王五']); editable.setCurrentIndex(1)
        spin = FocusSpinBox(); spin.setValue(10)
        for widget in (combo, editable, spin):
            layout.addWidget(widget)
        window.show(); window.activateWindow(); self.app.processEvents()
        def wheel(widget):
            event = QWheelEvent(QPointF(5, 5), QPointF(widget.mapToGlobal(QPoint(5, 5))),
                                QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(widget, event)
        for widget, get_value in ((combo, combo.currentIndex), (editable, editable.currentText), (spin, spin.value)):
            other.setFocus(); self.app.processEvents()
            before = get_value()
            wheel(widget)
            self.assertEqual(get_value(), before)
            widget.setFocus(); self.app.processEvents()
            wheel(widget)
            self.assertNotEqual(get_value(), before)
            self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.StrongFocus)
        window.hide(); window.deleteLater()

    def test_link_list_preserves_original_urls_and_html_clipboard(self):
        from PySide6.QtCore import QMimeData, Qt
        from transfer_widgets import LinkList
        box = LinkList()
        url = 'https://example.com/' + 'very-long-path-' * 50 + '?id=123&token=example'
        mime = QMimeData()
        mime.setText(url + '\nhttps://example.com/second')
        box.insertFromMimeData(mime)
        self.assertEqual(box.count(), 2)
        self.assertEqual(box.item(0).toolTip(), url)
        self.assertEqual(box.toPlainText().splitlines()[0], url)
        self.assertFalse(box.wordWrap())
        self.assertEqual(box.textElideMode(), Qt.TextElideMode.ElideRight)
        html = QMimeData(); html.setHtml('<a href="https://example.com/hidden">素材</a>')
        box.insertFromMimeData(html)
        self.assertEqual(box.item(2).text(), 'https://example.com/hidden')
        box.item(0).setSelected(True)
        box._copy()
        self.assertEqual(QApplication.clipboard().text(), url)
        box._delete()
        self.assertEqual(box.count(), 2)
        box.deleteLater()

    def test_upload_links_copy_and_log_adapts_to_remaining_space(self):
        page = self.page()
        page.resize(1240, 1400)
        page.show(); self.app.processEvents(); self.app.processEvents()
        page._adapt_upload_log()
        self.assertTrue(page.log_toggle.isChecked())
        large_log = page._upload_log_card.height()
        self.assertGreater(large_log, 200)
        self.assertTrue(page._receipt_card.isHidden())
        from PySide6.QtWidgets import QTableWidgetItem
        page.table.setRowCount(1)
        url = 'https://drive.google.com/file/d/example/view?original=1'
        page.table.setItem(0, 5, QTableWidgetItem(url))
        page.copy_table_link(0, 5)
        self.assertEqual(QApplication.clipboard().text(), url)
        page.resize(1240, 650)
        self.app.processEvents(); self.app.processEvents()
        page._adapt_upload_log()
        self.assertFalse(page.log_toggle.isChecked())
        self.assertTrue(page._upload_log_card.isHidden())
        page.resize(1240, 1400)
        self.app.processEvents(); self.app.processEvents()
        page._adapt_upload_log()
        self.assertTrue(page.log_toggle.isChecked())
        page.hide(); page.deleteLater()

    def test_ime_commit_does_not_start_recursive_table_edit(self):
        from transfer_widgets import SafeEditTable
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QInputMethodEvent
        from PySide6.QtWidgets import QTableWidgetItem, QLineEdit
        table = SafeEditTable(1, 1)
        table.setItem(0, 0, QTableWidgetItem('原表头'))
        table.setCurrentCell(0, 0)
        table.show(); self.app.processEvents()
        event = QInputMethodEvent(); event.setCommitString('中文')
        for _ in range(20):
            QApplication.sendEvent(table, event)
        self.assertEqual(table.item(0, 0).text(), '原表头')
        self.assertFalse(event.isAccepted())
        table.editItem(table.item(0, 0)); self.app.processEvents()
        editor = table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        commit = QInputMethodEvent(); commit.setCommitString('中文表头')
        QApplication.sendEvent(editor, commit)
        self.assertIn('中文表头', editor.text())
        table.hide(); table.deleteLater()

    def test_inbound_header_worker_reads_target_not_category(self):
        client = Mock()
        client.read_header_row.return_value = ['上传时间', '素材地址']
        worker = upload.InboundHeadersWorker('cred', 'token', 'sid', '目标入库表', 3)
        loaded = []; worker.loaded.connect(loaded.append)
        with patch.object(upload, 'GoogleClient', return_value=client):
            worker.run()
        client.read_header_row.assert_called_once_with('sid', '目标入库表', 3)
        self.assertEqual(loaded, [['上传时间', '素材地址']])

    def test_full_header_order_and_manual_column_survive_save(self):
        from column_mapping import mappings_from_headers
        headers = ['审核人员', '', '文件链接', '上传者', '业务备注'] + [''] * 22 + ['日期']
        rows = mappings_from_headers(headers)
        self.assertEqual([r['column'] for r in rows], ['A', 'C', 'D', 'E', 'AB'])
        self.assertEqual([r['header'] for r in rows], ['审核人员', '文件链接', '上传者', '业务备注', '日期'])
        page = self.page()
        self.assertEqual(page.mapping_editor.rows(), [])
        page.mapping_editor.set_rows(rows)
        page.mapping_editor.table.item(1, 0).setText('我自己的素材地址')
        page.mapping_editor.table.item(1, 1).setText('z')
        with patch.object(upload, 'settings_path', return_value=str(self.root / 'settings.json')):
            page.save_settings_and_collapse()
        restored = self.page()
        self.assertEqual(restored.mapping_editor.rows(), page.mapping_editor.rows())
        cells = mapped_cells(restored.mapping_editor.rows(), {'file_url': 'https://result'})
        self.assertEqual(cells['Z'], 'https://result')
        self.assertNotIn('C', cells)
        self.assertNotIn('E', cells)
        restored.deleteLater(); page.deleteLater()

    def test_read_full_header_row_preserves_blank_columns(self):
        client = object.__new__(sheets.GoogleClient)
        client.sheets = Mock()
        api = client.sheets.spreadsheets().values()
        api.get().execute.return_value = {'values': [['审核人员', '', '文件链接']]}
        self.assertEqual(client.read_header_row('sid', "入库'表", 4), ['审核人员', '', '文件链接'])
        self.assertEqual(api.get.call_args.kwargs['range'], "'入库''表'!4:4")

    def test_combo_popup_height_tracks_rows_and_scrolls(self):
        from focus_widgets import FocusComboBox
        from PySide6.QtWidgets import QWidget, QVBoxLayout
        container = QWidget(); layout = QVBoxLayout(container)
        combo = FocusComboBox(); layout.addWidget(combo)
        combo.setStyleSheet("QComboBox { min-height: 22px; max-height: 26px; padding: 3px 6px; combobox-popup: 0; }")
        combo.addItems(['Muya', 'Joyce'])
        container.show(); self.app.processEvents()
        combo.showPopup(); self.app.processEvents()
        self.assertLess(combo.view().window().height(), 150)
        self.assertGreater(combo.view().height(), 40)
        combo.hidePopup()
        combo.addItems([f'姓名{i}' for i in range(40)])
        combo.showPopup(); self.app.processEvents()
        self.assertLess(combo.view().window().height(), 400)
        self.assertGreater(combo.view().verticalScrollBar().maximum(), 0)
        combo.setCurrentIndex(41)
        self.assertEqual(combo.currentText(), '姓名39')
        combo.hidePopup(); container.hide(); container.deleteLater()

    def test_explicit_dropdown_target_controls_uploaded_cell(self):
        page = self.page()
        page.mapping_editor.set_rows([
            dict(header='国家', column='K', source='unassigned', value=''),
            dict(header='上传者', column='M', source='uploader', value='')])
        page.refresh_category_targets()
        page.category_targets[2].setCurrentIndex(page.category_targets[2].findData('K'))
        page.assign_category_target(2)
        values = mapped_cells(page.mapping_editor.rows(), {'cat3': '肯尼亚', 'uploader': 'Josephat'})
        self.assertEqual(values, {'K': '肯尼亚', 'M': 'Josephat'})
        with patch.object(upload, 'settings_path', return_value=str(self.root / 'settings.json')):
            page.save_settings_and_collapse()
        restored = self.page()
        self.assertEqual(restored.category_targets[2].currentData(), 'K')
        self.assertEqual(restored.category_targets[4].currentData(), 'M')
        restored.deleteLater(); page.deleteLater()

    def test_people_auto_detects_headers_without_assuming_column_positions(self):
        client = Mock(account_label='test')
        client.read_header_row.return_value = ['上传者', '类型', '国家', '进展', '', '审核人员']
        client.read_category_rows.return_value = [['Josephat', '无声视频', '肯尼亚', '待审', 'Josephat', 'Joyce']]
        client.read_mapped_headers.return_value = ['上传者', '类型', '国家', '进展', '上传者', '审核人员']
        worker = upload.CategoryLoadWorker('', '', 'sheet', '分类', ['A','B','C','D','',''])
        resolved = []; worker.columns_resolved.connect(resolved.append)
        with patch.object(upload, 'GoogleClient', return_value=client):
            worker.run()
        self.assertEqual(resolved, [['A', 'B', 'C', 'D', 'A', 'F']])
        client.read_category_rows.assert_called_once_with('sheet', '分类', ['A','B','C','D','A','F'], 2, True)

    def test_main_window_only_exposes_active_pages(self):
        from contextlib import ExitStack
        import sheets_batch_downloader_modern as main
        import video_batch_downloader as video
        import env_tools
        with ExitStack() as stack:
            for mod in (main, sheets, upload, paste, video, env_tools):
                stack.enter_context(patch.object(mod, 'user_data_directory', return_value=str(self.root)))
            stack.enter_context(patch.object(main.MainWindow, 'schedule_startup_update_check'))
            stack.enter_context(patch.object(video.VideoBatchPage, 'refresh_env_status'))
            window = main.MainWindow()
            self.assertEqual([window.main_tabs.tabText(i) for i in range(window.main_tabs.count())],
                             ['云端链接下载', 'YouTube / FB / TikTok / IG 视频', '批量上传云端'])
            window.show()
            self.app.processEvents()
            window.grab().save(str(self.root / 'cloud.png'))
            window.grab().save('/tmp/diy-final-cloud.png')
            before = window.paste_page.paste_box.height()
            window.resize(1240, 1400)
            self.app.processEvents(); self.app.processEvents()
            self.assertGreater(window.paste_page.paste_box.height() - before, 400)
            window.grab().save('/tmp/diy-tall-cloud.png')
            window.resize(1240, 820)
            self.app.processEvents()
            window.main_tabs.setCurrentWidget(window.upload_page)
            self.app.processEvents()
            window.grab().save('/tmp/diy-final-upload.png')
            self.assertTrue(window.upload_page.log_box.parentWidget().isHidden())
            self.assertFalse(window.upload_page.config_button.isHidden())
            window.hide()
            window.deleteLater()

    def test_mac_updater_prefers_matching_dmg(self):
        assets = [{'name': n} for n in ['app-macos-arm64.zip', 'app-macos-x86_64.dmg', 'app-macos-arm64.dmg']]
        with patch.object(updater.sys, 'platform', 'darwin'), patch.object(updater.platform, 'machine', return_value='arm64'):
            self.assertEqual(updater._pick_asset(assets), assets[2])


if __name__ == '__main__':
    unittest.main()
