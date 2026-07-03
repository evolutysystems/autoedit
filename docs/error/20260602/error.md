# Error Detail
TypeError: not enough arguments for format string 
Call stack:
  File "D:\develop\autoedit\src\main.py", line 96, in <module>
    main()
  File "D:\develop\autoedit\src\main.py", line 81, in main
    output = run_pipeline(path, settings, progress_cb=_console_progress)
  File "D:\develop\autoedit\src\pipeline\pipeline_runner.py", line 48, in run_pipeline
    concat_processor.run(context, "opening")      
  File "D:\develop\autoedit\src\modules\concat_processor.py", line 82, in run
    _logger.info("%s 開始: %s + %s", label, *(parts,))
Message: '%s 開始: %s + %s'
Arguments: ('オープニング結合', ['D:/StreamPipeline/dev/assets/op.mp4', 'C:\\Users\\evolu\\AppData\\Local\\Temp\\autoedit_4_3ii53g\\subtitle.mp4'])

# Output
- resolve.md
